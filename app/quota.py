"""Contador de cuota de PRIM por endpoint y dia UTC, persistido en SQLite.

Por que contar aqui si PRIM ya manda `x-ratelimit-remaining-day`: la cabecera
solo llega con cada respuesta. En la 0.3.0 eso tenia dos fallos
(docs/servidor.md 18.3, puntos 8 y 20): tras la medianoche UTC se seguia
viendo la cuota de ayer hasta la siguiente llamada, y el recolector, con la
de ayer por debajo de su reserva, no volvia a llamar nunca. Con un contador
propio por dia UTC el dia nuevo empieza solo, y ademas se puede frenar ANTES
de agotar (niveles y degradacion del TTL) en vez de enterarse con un 429.

  - `used` cuenta llamadas HTTP que llegaron a PRIM, por endpoint, dia UTC y
    clave. `key_id` es un hash corto de la clave: al cambiarla el contador
    empieza de cero, porque PRIM cuenta por clave.
  - Lo que queda es min(tope - used, lo que dice la cabecera): manda el mas
    pesimista de los dos.
  - Niveles por endpoint: < 70 % ok, < 85 % warn, < 95 % critical y si no
    exhausted. El global es el del peor endpoint.

SQLite: una escritura corta por llamada, siempre fuera del bucle de eventos
(prim.py llama a spend() en un hilo). Lo que se consulta en cada tablero
(can_spend, level, snapshot) sale de memoria.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import db
from .config import settings

log = logging.getLogger("trajet.quota")

ENDPOINTS = ("stop-monitoring", "general-message", "navitia")
LEVELS = ("ok", "warn", "critical", "exhausted")
_THRESHOLDS = ((0.70, "ok"), (0.85, "warn"), (0.95, "critical"))

# Cada cuanto conviene refrescar la pantalla segun el nivel.
REFRESH_HINT = {"ok": 30, "warn": 60, "critical": 120, "exhausted": 300}

# Degradacion elegante: con la cuota justa, el mismo dato se reutiliza mas
# tiempo. En exhausted aun quedan llamadas (del 95 % al 100 %), pero se
# guardan: como mucho una cada 10 min por dato.
TTL_FACTOR = {"ok": 1, "warn": 2, "critical": 4, "exhausted": 4}
TTL_MIN_EXHAUSTED = 600

# El ritmo de la pantalla lo marcan los endpoints del tablero. Si el
# planificador agota navitia, el tablero sigue igual de fresco: no tiene
# sentido que la app refresque cada 5 min por eso.
BOARD_ENDPOINTS = ("stop-monitoring", "general-message")

# Historial para el panel: se guardan 90 dias y se tiran los mas viejos.
KEEP_DAYS = 90


def key_id(key: str) -> str:
    """Hash corto de la clave: identifica el contador sin guardar la clave."""
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Quota:
    def __init__(self, cap_per_endpoint: int | None = None, key: str = "",
                 persist: bool = True, clock: Callable[[], datetime] | None = None):
        self.cap = max(1, int(cap_per_endpoint or settings.quota_cap))
        self._persist = persist
        self._clock = clock or _utc_now
        # _mem protege los contadores y solo se tiene unos microsegundos: lo
        # toma el bucle de eventos. _io ordena las escrituras en la BD (hilos).
        self._mem = threading.Lock()
        self._io = threading.Lock()
        self._warned = False
        self._key_id = key_id(key)
        self._day = self._today()
        self._used = dict.fromkeys(ENDPOINTS, 0)
        self._reported: dict[str, int | None] = dict.fromkeys(ENDPOINTS, None)
        self._load(prune=True)

    # ---------------- dia y persistencia ----------------

    def _today(self) -> str:
        return self._clock().astimezone(timezone.utc).date().isoformat()

    def _roll(self) -> None:
        """Dia UTC nuevo = contador nuevo. Llamar con _mem tomado."""
        today = self._today()
        if today != self._day:
            self._day = today
            self._used = dict.fromkeys(ENDPOINTS, 0)
            self._reported = dict.fromkeys(ENDPOINTS, None)

    def _warn_once(self, what: str, e: Exception) -> None:
        if not self._warned:
            self._warned = True
            log.warning("cuota de PRIM sin guardar en la BD (%s): %s", what,
                        type(e).__name__)

    def _load(self, prune: bool = False) -> None:
        """Lee de la BD el contador de hoy para la clave actual (reinicios)."""
        if not self._persist:
            return
        with self._mem:
            day, kid = self._day, self._key_id
        try:
            with db.conn() as c:
                rows = c.execute(
                    "SELECT endpoint, used, remaining_reported FROM quota_usage "
                    "WHERE day = ? AND key_id = ?", (day, kid)).fetchall()
                if prune:
                    corte = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
                    c.execute("DELETE FROM quota_usage WHERE day < ?", (corte,))
        except Exception as e:
            self._warn_once("lectura", e)
            return
        with self._mem:
            if (self._day, self._key_id) != (day, kid):
                return
            for r in rows:
                ep = r["endpoint"]
                if ep in self._used:
                    self._used[ep] = max(self._used[ep], int(r["used"] or 0))
                    if r["remaining_reported"] is not None:
                        self._reported[ep] = int(r["remaining_reported"])

    def _save(self, endpoint: str) -> None:
        """Escribe el valor ACTUAL (absoluto) de un endpoint.

        La foto se toma dentro de _io: si dos hilos escriben, el ultimo en
        escribir lleva el ultimo valor y la BD nunca va hacia atras.
        """
        if not self._persist:
            return
        with self._io:
            with self._mem:
                row = (self._day, endpoint, self._key_id, self._used[endpoint],
                       self._reported[endpoint])
            try:
                with db.conn() as c:
                    c.execute(
                        "INSERT INTO quota_usage "
                        "(day, endpoint, key_id, used, remaining_reported, updated_at) "
                        "VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(day, endpoint, key_id) DO UPDATE SET "
                        "used = excluded.used, "
                        "remaining_reported = excluded.remaining_reported, "
                        "updated_at = excluded.updated_at",
                        row + (_utc_now().isoformat(timespec="seconds"),))
            except Exception as e:
                self._warn_once("escritura", e)

    # ---------------- contar ----------------

    def _remaining(self, endpoint: str) -> int:
        rem = self.cap - self._used[endpoint]
        rep = self._reported[endpoint]
        if rep is not None:
            rem = min(rem, rep)
        return max(0, rem)

    def _level(self, endpoint: str) -> str:
        spent = (self.cap - self._remaining(endpoint)) / self.cap
        for limit, name in _THRESHOLDS:
            if spent < limit:
                return name
        return "exhausted"

    def can_spend(self, endpoint: str) -> bool:
        """False si ya no queda nada hoy (tope local o lo que dice PRIM)."""
        with self._mem:
            self._roll()
            return self._remaining(endpoint) > 0

    def spend(self, endpoint: str, remaining_reported: int | None = None) -> None:
        """Una llamada HTTP real hecha. Escribe en la BD: llamar fuera del bucle."""
        if endpoint not in ENDPOINTS:
            return
        with self._mem:
            self._roll()
            self._used[endpoint] += 1
            if remaining_reported is not None:
                self._reported[endpoint] = max(0, int(remaining_reported))
        self._save(endpoint)

    def observe(self, endpoint: str, remaining_reported: int | None) -> None:
        """Apunta lo que dice PRIM sin contar una llamada (las de comprobar
        la clave, que no son trafico de la app)."""
        if endpoint not in ENDPOINTS or remaining_reported is None:
            return
        with self._mem:
            self._roll()
            self._reported[endpoint] = max(0, int(remaining_reported))
        self._save(endpoint)

    def mark_exhausted(self, endpoint: str) -> None:
        """PRIM ha dicho 429 de cuota diaria: no se le vuelve a llamar hasta
        la medianoche UTC (se guarda como 0 restantes)."""
        if endpoint not in ENDPOINTS:
            return
        with self._mem:
            self._roll()
            self._reported[endpoint] = 0
        self._save(endpoint)

    def reset_for_new_key(self, key: str | None = None) -> None:
        """Al cambiar la clave. El contador pasa a ser el de la clave nueva:
        de cero si nunca se uso hoy, o el que llevara si se vuelve a una."""
        with self._mem:
            if key is not None:
                self._key_id = key_id(key)
            self._day = self._today()
            self._used = dict.fromkeys(ENDPOINTS, 0)
            self._reported = dict.fromkeys(ENDPOINTS, None)
        self._load()

    # ---------------- consultar ----------------

    def remaining(self, endpoint: str) -> int:
        """Llamadas que quedan hoy para ese endpoint (la cifra efectiva)."""
        with self._mem:
            self._roll()
            return self._remaining(endpoint)

    def used(self, endpoint: str) -> int:
        with self._mem:
            self._roll()
            return self._used[endpoint]

    def level(self, endpoint: str | None = None) -> str:
        """Nivel de un endpoint, o el del peor si no se dice cual."""
        with self._mem:
            self._roll()
            if endpoint is not None:
                return self._level(endpoint)
            return max((self._level(ep) for ep in ENDPOINTS), key=LEVELS.index)

    def refresh_hint(self) -> int:
        """30/60/120/300 s segun el peor endpoint del tablero."""
        with self._mem:
            self._roll()
            worst = max((self._level(ep) for ep in BOARD_ENDPOINTS), key=LEVELS.index)
        return REFRESH_HINT[worst]

    def ttl_for(self, ttl: float, endpoint: str) -> float:
        """TTL efectivo de un dato de ese endpoint segun lo justa que vaya
        su cuota: x2 en warn, x4 en critical y 10 min como poco en exhausted."""
        lvl = self.level(endpoint) if endpoint in ENDPOINTS else "ok"
        out = ttl * TTL_FACTOR[lvl]
        if lvl == "exhausted":
            out = max(out, TTL_MIN_EXHAUSTED)
        return out

    def snapshot(self) -> dict:
        """QuotaV1 de docs/openapi.yaml."""
        with self._mem:
            self._roll()
            now = self._clock().astimezone(timezone.utc)
            resets = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
            eps = [{"endpoint": ep, "used": self._used[ep], "cap": self.cap,
                    "remaining_reported": self._reported[ep],
                    "level": self._level(ep)} for ep in ENDPOINTS]
            day = self._day
        worst = max((e["level"] for e in eps), key=LEVELS.index)
        return {"day_utc": day, "resets_at": resets.isoformat(), "level": worst,
                "refresh_hint_s": self.refresh_hint(), "endpoints": eps}

    def as_legacy(self) -> dict[str, int]:
        """El `quota` de la 0.3.0 (endpoint -> llamadas que quedan): solo los
        endpoints que ya se han usado hoy, como antes solo salian los que
        habian respondido."""
        with self._mem:
            self._roll()
            return {ep: self._remaining(ep) for ep in ENDPOINTS
                    if self._used[ep] or self._reported[ep] is not None}

    def history(self, days: int = 7) -> list[dict]:
        """AdminQuotaDay: llamadas por dia UTC y endpoint de los ultimos
        `days` dias (hoy incluido), sumando todas las claves. Denso: los
        dias sin llamadas salen con 0, para poder pintarlo sin huecos."""
        days = max(1, min(int(days), KEEP_DAYS))
        with self._mem:
            self._roll()
            today = date.fromisoformat(self._day)
            mem_today = dict(self._used)
        first = today - timedelta(days=days - 1)
        found: dict[tuple[str, str], int] = {}
        try:
            with db.conn() as c:
                for r in c.execute(
                        "SELECT day, endpoint, SUM(used) AS n FROM quota_usage "
                        "WHERE day >= ? AND day <= ? GROUP BY day, endpoint",
                        (first.isoformat(), today.isoformat())):
                    found[(r["day"], r["endpoint"])] = int(r["n"] or 0)
        except Exception as e:
            self._warn_once("historial", e)
        out = []
        for i in range(days):
            d = (first + timedelta(days=i)).isoformat()
            for ep in ENDPOINTS:
                n = found.get((d, ep), 0)
                if d == today.isoformat():
                    # Si la BD va por detras (o no se pudo escribir), manda
                    # lo que hay en memoria para la clave en uso.
                    n = max(n, mem_today[ep])
                out.append({"day_utc": d, "endpoint": ep, "used": n})
        return out


# ---------------- accesor de modulo ----------------

_quota: Quota | None = None
_quota_sig: tuple | None = None


def get_quota() -> Quota:
    """El contador de la configuracion actual (se rehace si cambian la BD o
    el tope, como pasa entre tests)."""
    global _quota, _quota_sig
    sig = (os.path.abspath(settings.db_path), settings.quota_cap)
    if _quota is None or _quota_sig != sig:
        _quota = Quota(settings.quota_cap)
        _quota_sig = sig
    return _quota


def reset_quota() -> None:
    global _quota, _quota_sig
    _quota = None
    _quota_sig = None
