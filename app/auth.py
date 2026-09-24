"""Emparejamiento, dispositivos, tokens y quien puede hablar con el panel.

Tres piezas (docs/servidor-v2.md, «auth»; docs/openapi.yaml):

  - Emparejar: el panel genera un codigo de un solo uso (5 min) que va en el
    QR; el iPhone lo canjea en /api/v1/pair por un token de dispositivo.
  - Token: `trj_` + 256 bits aleatorios. En la BD solo su SHA-256: si alguien
    se lleva una copia de trajet.db no se lleva tokens que funcionen.
  - Panel: ademas del login de Umbrel, se comprueba que la conexion venga del
    proxy de Umbrel y, en lo que modifica, la cabecera anti-CSRF y el origen.

Las funciones que tocan SQLite son sincronas: las rutas las llaman con
run_in_threadpool para no bloquear el bucle de eventos. Las dos dependencias
de FastAPI (require_device y require_panel) ya lo hacen por su cuenta.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import ipaddress
import logging
import math
import os
import re
import secrets
import socket
import sqlite3
import struct
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.concurrency import run_in_threadpool

from . import db
from .api.errors import ApiError
from .config import VERSION, settings

log = logging.getLogger("trajet.auth")

PAIR_TTL = 300                                  # 5 minutos (ttl_s del contrato)
# Sin 0/O ni 1/I: el codigo se lee en una pantalla y se puede teclear a mano.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 8
TOKEN_PREFIX = "trj_"                           # noqa: S105 (prefijo, no un secreto)
_TOKEN_RE = re.compile(r"^trj_[A-Za-z0-9_-]{43}$")

# Rate limit del canje (en memoria: si se reinicia el contenedor se pierde, y
# lo unico que pasa es que el atacante gana una ventana; los codigos siguen
# caducando a los 5 min).
#
# Compromiso aceptado (SEC-4 de la verificacion de la FASE 1): el cupo global
# y la anulacion tras MAX_CONSECUTIVE_FAILS fallos seguidos los puede disparar
# cualquiera que llegue a /api/v1/pair sin token (otra app de la red Docker o
# alguien de la LAN). Con eso puede ESTORBAR el emparejamiento del dueno (su
# canje recibe 429 unos minutos, o el QR que acaba de generar queda anulado y
# hay que sacar otro), pero no puede robar nada ni conseguir un token. Se
# prefiere asi: un cupo por codigo o por IP dejaria probar mucho mas con IPs
# cambiantes, y el global es justo lo que hace inutil la fuerza bruta contra
# 2^40 codigos que caducan a los 5 min. Si el dueno ve «demasiados intentos»,
# basta con esperar y generar otro QR.
RATE_PER_IP = 5
RATE_PER_IP_WINDOW = 60
RATE_GLOBAL = 20
RATE_GLOBAL_WINDOW = 300
MAX_CONSECUTIVE_FAILS = 10

# last_used_at se apunta como mucho una vez por minuto: el iPhone pide el
# tablero cada 30 s y no merece la pena una escritura en SQLite cada vez.
TOUCH_EVERY = 60

MAX_DEVICE_NAME = 60
MAX_DEVICE_MODEL = 60
MAX_APP_VERSION = 30

# Donde leer la puerta de enlace del contenedor (se cambia en los tests).
PROC_ROUTE = "/proc/net/route"
_PEERS_TTL = 30.0


# ---------------- utilidades ----------------

def _now() -> datetime:
    """Hora UTC. Una funcion aparte para poder moverla en los tests."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_text(value, limit: int) -> str:
    """Texto que se va a ensenar en el panel: sin caracteres de control y con
    tope de longitud (el HTML ya escapa, esto es para que no se descoloque)."""
    if not isinstance(value, str):
        return ""
    value = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(value.split())[:limit]


def normalize_code(code) -> str:
    """ABCD-EFGH, abcd efgh y ABCDEFGH son el mismo codigo."""
    if not isinstance(code, str):
        return ""
    return re.sub(r"[\s\-_]", "", code).upper()


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


# ---------------- direcciones y nombre del servidor ----------------

def _setting(key: str, fallback: str) -> str:
    """Lo del panel manda; si no hay nada guardado, el valor del entorno.

    Una fila guardada vacia es «no hay» a proposito (el panel puede quitar
    una direccion aunque este en el entorno)."""
    try:
        v = db.get_setting(key)
    except sqlite3.Error:
        v = None
    return fallback if v is None else v


def _valid_base_url(url: str) -> str | None:
    url = (url or "").strip().rstrip("/")
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


def server_name() -> str:
    name = _clean_text(_setting("server_name", settings.server_name), 40)
    return name or "Trajet"


def server_urls() -> list[dict]:
    """Direcciones del servidor (ServerUrl del contrato) que van en el QR.

    Nunca hay IPs fijas aqui: salen del panel (settings_kv) o del entorno
    (TRAJET_LAN_URL, TRAJET_TAILSCALE_URL)."""
    out = []
    for kind, key, fallback in (("lan", "lan_url", settings.lan_url),
                                ("tailscale", "tailscale_url", settings.tailscale_url)):
        url = _valid_base_url(_setting(key, fallback))
        if url:
            out.append({"kind": kind, "url": url})
    return out


def server_info() -> dict:
    """El bloque `server` de PairResult."""
    return {"name": server_name(), "version": VERSION, "urls": server_urls()}


def qr_payload(code_display: str, urls: list[dict], name: str) -> str:
    """trajet://pair?v=1&code=ABCD-EFGH&lan=<url>&ts=<url>&name=<nombre>

    Todo va con percent-encoding completo (safe=''), asi que una URL con
    `:` o `/` no rompe el enlace. Nunca lleva la clave PRIM ni el token."""
    parts = [("v", "1"), ("code", code_display)]
    by_kind = {u["kind"]: u["url"] for u in urls}
    if by_kind.get("lan"):
        parts.append(("lan", by_kind["lan"]))
    if by_kind.get("tailscale"):
        parts.append(("ts", by_kind["tailscale"]))
    parts.append(("name", name))
    return "trajet://pair?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in parts)


def qr_svg(payload: str) -> str:
    """QR en SVG para incrustar en el panel (segno: sin dependencias y sin
    CDN, el panel tiene que funcionar sin internet). Sin width/height fijos
    (solo viewBox) para que el panel lo escale con CSS; fondo blanco para que
    se lea tambien con el panel en modo oscuro."""
    import segno

    qr = segno.make_qr(payload, error="m")
    return qr.svg_inline(scale=4, border=4, omitsize=True, dark="#000", light="#fff",
                         title="Código QR para emparejar Trajet")


# ---------------- emparejamiento ----------------

def _pairing_status_row(row: sqlite3.Row, now: datetime) -> str:
    if row["used_at"]:
        return "used"
    if row["cancelled_at"]:
        return "cancelled"
    exp = _parse_iso(row["expires_at"])
    if exp is None or exp <= now:
        return "expired"
    return "pending"


def _cancel_pending(c: sqlite3.Connection, now: datetime) -> int:
    cur = c.execute(
        "UPDATE pairing_codes SET cancelled_at = ? "
        "WHERE used_at IS NULL AND cancelled_at IS NULL AND expires_at > ?",
        (_iso(now), _iso(now)))
    return cur.rowcount


def new_pairing(urls: list[dict] | None = None) -> dict:
    """Genera un codigo nuevo (PairingSession del contrato).

    Anula el que siguiera vivo: solo hay un QR valido a la vez, el ultimo que
    se ha ensenado. Del codigo solo se guarda su SHA-256; en claro sale solo
    en esta respuesta (y en el QR)."""
    now = _now()
    expires = now + timedelta(seconds=PAIR_TTL)
    urls = server_urls() if urls is None else urls
    name = server_name()

    with db.conn() as c:
        _cancel_pending(c, now)
        # Limpieza: los codigos viejos no sirven para nada (el panel solo
        # sondea el ultimo), asi que no se acumulan para siempre.
        c.execute("DELETE FROM pairing_codes WHERE expires_at < ?",
                  (_iso(now - timedelta(days=7)),))
        for _ in range(5):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            pid = secrets.token_urlsafe(12)
            try:
                c.execute(
                    "INSERT INTO pairing_codes (id, code_hash, created_at, expires_at) "
                    "VALUES (?,?,?,?)", (pid, _sha256(code), _iso(now), _iso(expires)))
                break
            except sqlite3.IntegrityError:     # choque de hash o de id: otro
                continue
        else:                                  # pragma: no cover (2^40 codigos)
            raise ApiError("internal", "no se pudo generar el codigo", status=500)

    display = format_code(code)
    payload = qr_payload(display, urls, name)
    log.info("codigo de emparejamiento nuevo (id %s), caduca a las %s", pid, _iso(expires))
    return {
        "id": pid,
        "code": display,
        "expires_at": _iso(expires),
        "ttl_s": PAIR_TTL,
        "qr_payload": payload,
        "qr_svg": qr_svg(payload),
        "urls": urls,
    }


def _pairing_dict(c: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> dict:
    out = {"id": row["id"], "status": _pairing_status_row(row, now),
           "expires_at": row["expires_at"]}
    if row["device_id"] is not None:
        dev = c.execute("SELECT * FROM devices WHERE id = ?", (row["device_id"],)).fetchone()
        if dev is not None:
            out["device"] = _device_dict(dev)
    return out


def pairing_status(pairing_id: str) -> dict:
    """PairingStatus del contrato. ApiError not_found si no existe."""
    now = _now()
    with db.conn() as c:
        row = c.execute("SELECT * FROM pairing_codes WHERE id = ?",
                        (str(pairing_id),)).fetchone()
        if row is None:
            raise ApiError("not_found", "no existe ese emparejamiento")
        return _pairing_dict(c, row, now)


def cancel_pairing(pairing_id: str) -> dict:
    """Anula un QR que siga pendiente. Si ya estaba usado, caducado o anulado
    se deja como esta y se devuelve su estado."""
    now = _now()
    with db.conn() as c:
        c.execute(
            "UPDATE pairing_codes SET cancelled_at = ? WHERE id = ? "
            "AND used_at IS NULL AND cancelled_at IS NULL AND expires_at > ?",
            (_iso(now), str(pairing_id), _iso(now)))
        row = c.execute("SELECT * FROM pairing_codes WHERE id = ?",
                        (str(pairing_id),)).fetchone()
        if row is None:
            raise ApiError("not_found", "no existe ese emparejamiento")
        return _pairing_dict(c, row, now)


def cancel_all_pending() -> int:
    with db.conn() as c:
        return _cancel_pending(c, _now())


# ---------------- rate limit del canje ----------------

class _PairLimiter:
    """Ventanas deslizantes en memoria: 5 intentos/min por IP y 20 cada
    5 min en total, y la cuenta de fallos seguidos.

    El limite global es el que de verdad para la fuerza bruta: con IPs
    cambiantes el de por IP no sirve de nada, pero 20 intentos cada 5 min
    contra 2^40 codigos que caducan a los 5 min es no acertar nunca."""

    MAX_IPS = 10_000

    def __init__(self):
        self.lock = threading.Lock()
        self.by_ip: dict[str, deque] = {}
        self.all: deque = deque()
        self.fails = 0

    def reset(self) -> None:
        with self.lock:
            self.by_ip.clear()
            self.all.clear()
            self.fails = 0
            self.warned_at = None

    warned_at: float | None = None

    def should_warn(self, now: float) -> bool:
        """Un aviso de «demasiados intentos» como mucho por minuto: si no,
        quien insiste llenaria error_log y taparia los errores de verdad."""
        with self.lock:
            if self.warned_at is not None and now - self.warned_at < 60:
                return False
            self.warned_at = now
            return True

    def attempt(self, ip: str, now: float) -> int | None:
        """Cuenta un intento. Devuelve None si se permite o los segundos que
        hay que esperar (y entonces NO cuenta: si no, quien insiste se
        alargaria el castigo a si mismo sin fin y tambien a los demas)."""
        with self.lock:
            while self.all and now - self.all[0] >= RATE_GLOBAL_WINDOW:
                self.all.popleft()
            q = self.by_ip.get(ip)
            if q is not None:
                while q and now - q[0] >= RATE_PER_IP_WINDOW:
                    q.popleft()
            wait = 0.0
            if q is not None and len(q) >= RATE_PER_IP:
                wait = max(wait, RATE_PER_IP_WINDOW - (now - q[0]))
            if len(self.all) >= RATE_GLOBAL:
                wait = max(wait, RATE_GLOBAL_WINDOW - (now - self.all[0]))
            if wait > 0:
                return max(1, math.ceil(wait))
            if q is None:
                if len(self.by_ip) >= self.MAX_IPS:
                    self._prune(now)
                q = self.by_ip.setdefault(ip, deque())
            q.append(now)
            self.all.append(now)
            return None

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self.by_ip.items()
                  if not q or now - q[-1] >= RATE_PER_IP_WINDOW]:
            del self.by_ip[k]
        while len(self.by_ip) >= self.MAX_IPS:       # todas recientes: la mas vieja fuera
            self.by_ip.pop(next(iter(self.by_ip)))

    def failed(self) -> bool:
        """Apunta un fallo. True si toca anular todos los codigos vivos."""
        with self.lock:
            self.fails += 1
            if self.fails >= MAX_CONSECUTIVE_FAILS:
                self.fails = 0
                return True
            return False

    def succeeded(self) -> None:
        with self.lock:
            self.fails = 0


_limiter = _PairLimiter()


def _mono() -> float:
    return time.monotonic()


def _invalid() -> ApiError:
    # MISMA respuesta para mal escrito, caducado, usado o anulado: no se da
    # ninguna pista de cual de los casos es.
    return ApiError("pairing_invalid",
                    "el código no vale: puede estar mal escrito, caducado o ya usado; "
                    "genera otro en el panel")


def redeem(code, device_name, device_model="", app_version="", ip: str = "") -> dict:
    """Canjea un codigo por un token de dispositivo (PairResult del contrato).

    Un solo uso: marcar el codigo como usado y crear el dispositivo van en la
    misma transaccion, y la marca solo se pone si nadie la ha puesto antes
    (dos canjes a la vez del mismo codigo: solo uno gana)."""
    now_mono = _mono()
    wait = _limiter.attempt(ip or "?", now_mono)
    if wait is not None:
        if _limiter.should_warn(now_mono):
            log.warning("emparejamiento: demasiados intentos (el ultimo desde %s); "
                        "se rechazan durante %d s", ip or "?", wait)
        raise ApiError("rate_limited",
                       f"demasiados intentos de emparejar; vuelve a probar en {wait} s",
                       retry_after=wait)

    name = _clean_text(device_name, MAX_DEVICE_NAME)
    if not name:
        raise ApiError("bad_request", "falta el nombre del dispositivo")
    model = _clean_text(device_model, MAX_DEVICE_MODEL)
    appv = _clean_text(app_version, MAX_APP_VERSION)

    norm = normalize_code(code)
    device = None
    token = None
    now = _now()
    if len(norm) == CODE_LEN and all(ch in CODE_ALPHABET for ch in norm):
        wanted = _sha256(norm)
        with db.conn() as c:
            row = c.execute(
                "SELECT id, code_hash, expires_at FROM pairing_codes WHERE code_hash = ? "
                "AND used_at IS NULL AND cancelled_at IS NULL", (wanted,)).fetchone()
            exp = _parse_iso(row["expires_at"]) if row is not None else None
            if (row is not None and hmac.compare_digest(row["code_hash"], wanted)
                    and exp is not None and exp > now):
                cur = c.execute(
                    "UPDATE pairing_codes SET used_at = ? WHERE id = ? "
                    "AND used_at IS NULL AND cancelled_at IS NULL", (_iso(now), row["id"]))
                if cur.rowcount == 1:
                    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
                    cur = c.execute(
                        "INSERT INTO devices (name, model, app_version, token_hash, "
                        " created_at, last_used_at, last_ip) VALUES (?,?,?,?,?,?,?)",
                        (name, model, appv, _sha256(token), _iso(now), _iso(now), ip or None))
                    dev_id = int(cur.lastrowid)
                    c.execute("UPDATE pairing_codes SET device_id = ? WHERE id = ?",
                              (dev_id, row["id"]))
                    device = _device_dict(c.execute(
                        "SELECT * FROM devices WHERE id = ?", (dev_id,)).fetchone())

    if device is None or token is None:
        if _limiter.failed():
            anulados = cancel_all_pending()
            log.warning("emparejamiento: %d fallos seguidos; se anulan los codigos vivos (%d)",
                        MAX_CONSECUTIVE_FAILS, anulados)
        raise _invalid()

    _limiter.succeeded()
    log.info("dispositivo emparejado: id %d (%s)", device["id"], device["name"])
    return {"token": token, "device": public_device(device), "server": server_info()}


# ---------------- dispositivos y tokens ----------------

def _device_dict(row: sqlite3.Row) -> dict:
    """Device del contrato, con last_ip (que solo debe llegar al panel)."""
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "model": row["model"] or "",
        "app_version": row["app_version"] or "",
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "last_ip": row["last_ip"],
    }


def public_device(device: dict) -> dict:
    """El dispositivo tal como lo ve el propio iPhone: sin la IP."""
    return {k: v for k, v in device.items() if k != "last_ip"}


def verify_token(token: str | None, ip: str | None = None, touch: bool = True) -> dict | None:
    """Dispositivo del token, o None si no vale o esta revocado.

    Se busca por el SHA-256 (indice UNIQUE) y se confirma con compare_digest,
    en tiempo constante. Con touch=True apunta el ultimo uso y la IP, como
    mucho una vez por minuto."""
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    wanted = _sha256(token)
    with db.conn() as c:
        row = c.execute("SELECT * FROM devices WHERE token_hash = ?", (wanted,)).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], wanted):
            return None
        if row["revoked_at"]:
            return None
        device = _device_dict(row)
        if touch:
            now = _now()
            last = _parse_iso(row["last_used_at"])
            if last is None or (now - last).total_seconds() >= TOUCH_EVERY:
                c.execute("UPDATE devices SET last_used_at = ?, last_ip = ? WHERE id = ?",
                          (_iso(now), ip or row["last_ip"], row["id"]))
                device["last_used_at"] = _iso(now)
                device["last_ip"] = ip or row["last_ip"]
    return device


def list_devices() -> list[dict]:
    """Dispositivos emparejados activos (los revocados ya no cuentan)."""
    with db.conn() as c:
        rows = c.execute("SELECT * FROM devices WHERE revoked_at IS NULL "
                         "ORDER BY id").fetchall()
    return [_device_dict(r) for r in rows]


def active_device_count() -> int:
    with db.conn() as c:
        return int(c.execute(
            "SELECT COUNT(*) FROM devices WHERE revoked_at IS NULL").fetchone()[0])


def get_device(device_id: int) -> dict | None:
    """Un dispositivo activo, o None (tambien si esta revocado)."""
    with db.conn() as c:
        row = c.execute("SELECT * FROM devices WHERE id = ? AND revoked_at IS NULL",
                        (int(device_id),)).fetchone()
    return _device_dict(row) if row is not None else None


def rename_device(device_id: int, name) -> dict:
    """Cambia el nombre. ApiError bad_request (nombre vacio o de mas de 60
    caracteres) o not_found."""
    if not isinstance(name, str) or len(name.strip()) > MAX_DEVICE_NAME:
        raise ApiError("bad_request", f"el nombre tiene que tener de 1 a {MAX_DEVICE_NAME} caracteres")
    clean = _clean_text(name, MAX_DEVICE_NAME)
    if not clean:
        raise ApiError("bad_request", f"el nombre tiene que tener de 1 a {MAX_DEVICE_NAME} caracteres")
    with db.conn() as c:
        cur = c.execute("UPDATE devices SET name = ? WHERE id = ? AND revoked_at IS NULL",
                        (clean, int(device_id)))
        if cur.rowcount == 0:
            raise ApiError("not_found", "no existe ese dispositivo")
        row = c.execute("SELECT * FROM devices WHERE id = ?", (int(device_id),)).fetchone()
    return _device_dict(row)


def revoke_device(device_id: int) -> bool:
    """Revoca el token. Desde ese momento verify_token ya no lo acepta (no hay
    cache de tokens en memoria que esperar). False si no existia o ya estaba
    revocado."""
    with db.conn() as c:
        cur = c.execute("UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (_iso(_now()), int(device_id)))
        ok = cur.rowcount > 0
    if ok:
        log.info("dispositivo revocado: id %d", int(device_id))
    return ok


# ---------------- de donde viene la conexion ----------------

def _ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        addr = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _default_gateway() -> str | None:
    """Puerta de enlace por defecto del contenedor, de /proc/net/route.

    En umbreld 2.0 el proxy de Umbrel (app-gateway) va dentro del propio
    umbreld y las peticiones llegan al contenedor desde la puerta de enlace
    de la red Docker (docs/decisiones.md D0.6)."""
    try:
        with open(PROC_ROUTE, encoding="ascii") as f:
            next(f, None)                          # cabecera
            for line in f:
                parts = line.split()
                if len(parts) < 4 or parts[1] != "00000000":
                    continue
                if int(parts[3], 16) & 0x2:        # RTF_GATEWAY
                    return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    except (OSError, ValueError, struct.error):
        return None
    return None


def _resolve(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return set()
    return {str(i[4][0]) for i in infos}


_peers_cache: tuple[float, frozenset] | None = None
_peers_lock = threading.Lock()


def _auto_peers() -> frozenset:
    """127.0.0.1, ::1, la puerta de enlace y lo que resuelva APP_PROXY_HOSTNAME.

    Se guarda 30 s: resolver un nombre en cada peticion del panel no merece
    la pena y un DNS lento no debe colgarlo."""
    global _peers_cache
    now = _mono()
    with _peers_lock:
        if _peers_cache and now - _peers_cache[0] < _PEERS_TTL:
            return _peers_cache[1]
    found = {"127.0.0.1", "::1"}
    gw = _default_gateway()
    if gw:
        found.add(gw)
    host = os.environ.get("APP_PROXY_HOSTNAME", "").strip()
    if host:
        found |= _resolve(host)
    peers = frozenset(str(a) for a in (_ip(x) for x in found) if a is not None)
    with _peers_lock:
        _peers_cache = (now, peers)
    return peers


@functools.lru_cache(maxsize=8)
def _networks(spec: str) -> tuple:
    # Cacheado por texto: el aviso de una entrada mala sale una vez, no en
    # cada peticion del panel.
    nets = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning("TRAJET_ADMIN_PEERS: '%s' no es una red valida; se ignora", part)
    return tuple(nets)


def _mode() -> str:
    return (settings.admin_peers or "auto").strip().lower()


def is_trusted_proxy(peer: str | None) -> bool:
    """La conexion viene del proxy de Umbrel (o de 127.0.0.1).

    Siempre el conjunto de «auto», sea cual sea TRAJET_ADMIN_PEERS: esa
    variable decide quien entra al panel, no quien puede escribir
    X-Forwarded-For. Con «any» o con la red de casa, cualquiera se inventaria
    la IP para saltarse el limite por IP del emparejamiento."""
    addr = _ip(peer)
    return addr is not None and str(addr) in _auto_peers()


def peer_allowed_for_panel(peer: str | None) -> bool:
    mode = _mode()
    if mode == "any":
        return True
    addr = _ip(peer)
    if addr is None:
        return False
    if mode == "auto":
        return str(addr) in _auto_peers()
    return any(addr in n for n in _networks(mode) if n.version == addr.version)


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


def client_ip(request: Request) -> str:
    """IP de quien hace la peticion.

    X-Forwarded-For solo cuenta si la conexion viene del proxy; si no, lo
    puede escribir cualquiera. Se toma el ULTIMO valor, el que ha anadido
    nuestro proxy: si el proxy anade en vez de sustituir, el primero lo pone
    el cliente y se lo puede inventar."""
    peer = _peer(request)
    xff = request.headers.get("x-forwarded-for")
    if xff and is_trusted_proxy(peer):
        values = [v.strip() for v in xff.split(",") if v.strip()]
        if values and _ip(values[-1]) is not None:
            return str(_ip(values[-1]))
    return peer


# ---------------- dependencias de FastAPI ----------------

def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    scheme, _, value = auth.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


_WWW = {"WWW-Authenticate": 'Bearer realm="trajet"'}


def _verify_request(request: Request, token: str) -> dict | None:
    # client_ip puede resolver APP_PROXY_HOSTNAME (DNS): va en el mismo hilo
    # que la consulta a SQLite, fuera del bucle de eventos.
    return verify_token(token, client_ip(request))


async def require_device(request: Request) -> dict:
    """Dependencia de /api/v1: el dispositivo del token o 401 `unauthorized`.

    Deja el dispositivo en request.state.device."""
    token = _bearer(request)
    if token is None:
        raise ApiError("unauthorized", "falta el token del dispositivo; empareja el iPhone "
                       "desde el panel", headers=_WWW)
    device = await run_in_threadpool(_verify_request, request, token)
    if device is None:
        raise ApiError("unauthorized", "el token no vale o el dispositivo esta revocado; "
                       "vuelve a emparejar el iPhone", headers=_WWW)
    request.state.device = device
    return device


async def optional_device(request: Request) -> dict | None:
    """Para /api/v1/ping: el dispositivo si el token vale, sin 401 y sin
    escribir nada en la BD."""
    token = _bearer(request)
    if token is None:
        return None
    return await run_in_threadpool(verify_token, token, None, False)


_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


async def _require_peer(request: Request, what: str) -> str:
    """La conexion (no X-Forwarded-For) viene de un par permitido por
    TRAJET_ADMIN_PEERS, o ApiError forbidden. Devuelve el par.

    En Umbrel eso es el proxy (app-gateway de umbreld) o 127.0.0.1: lo que
    llega por el proxy ya ha pasado el login de Umbrel. Cualquier otra app de
    la red Docker compartida (umbrel_main_network) llega DIRECTA a web:8000 y
    se saltaria ese login; por eso se mira la conexion y no una cabecera."""
    peer = _peer(request)
    allowed = await run_in_threadpool(peer_allowed_for_panel, peer)
    if not allowed:
        raise ApiError(
            "forbidden",
            f"{what} solo acepta conexiones del proxy de Umbrel y esta llega de "
            f"{peer or 'origen desconocido'}; si entras por otro camino, ajusta "
            f"TRAJET_ADMIN_PEERS (any, o la red de la que vienes, p. ej. 192.168.1.0/24)")
    return peer


async def require_proxy_peer(request: Request) -> None:
    """Dependencia de la API de la 0.3.0 (/api/*): solo la comprobacion del
    par, la misma que la del panel.

    Esa API no tiene token: se apoya en el login de Umbrel
    (PROXY_AUTH_ADD), igual que el panel, y tiene el mismo agujero si no se
    mira de donde viene la conexion: otra app de la red Docker podria leer
    las rutas (con la direccion de casa en origin_id/origin_name), borrarlas
    o gastar cuota de PRIM (SEC-1 de la verificacion de la FASE 1).

    SIN la cabecera anti-CSRF X-Trajet-Panel del panel: los clientes de la
    0.3.0 (la app del iPhone de antes) no la mandan y tienen que seguir
    funcionando. Frente a otras webs queda igual que en la 0.3.0."""
    await _require_peer(request, "la API de la 0.3.0")


async def require_panel(request: Request) -> None:
    """Dependencia de /api/admin: de donde viene la conexion y anti-CSRF.

    1) La conexion (no X-Forwarded-For) tiene que venir de un par permitido
       por TRAJET_ADMIN_PEERS: `auto` (proxy de Umbrel o 127.0.0.1), `any`
       o una lista de redes.
    2) En lo que modifica: cabecera `X-Trajet-Panel: 1` (un formulario de
       otra web no puede ponerla) y, si llega `Origin`, que sea este mismo
       servidor."""
    peer = await _require_peer(request, "el panel")
    if request.method.upper() not in _UNSAFE:
        return None
    if request.headers.get("x-trajet-panel", "").strip() != "1":
        raise ApiError("forbidden", "falta la cabecera X-Trajet-Panel: 1 (protege el panel "
                       "de formularios de otras webs)")
    origin = request.headers.get("origin")
    if origin is not None:
        origin_host = (urlsplit(origin.strip()).netloc or "").lower()
        hosts = {(request.headers.get("host") or "").strip().lower()}
        # X-Forwarded-Host solo cuenta si la conexion viene del proxy de
        # confianza (is_trusted_proxy: siempre el conjunto «auto», sea cual
        # sea TRAJET_ADMIN_PEERS). De cualquier otro par es una cabecera que
        # escribe el cliente, y con ella y un Origin a juego se colaria
        # cualquier origen (SEC-3). Del proxy si vale: el app-gateway de
        # umbreld 2.0 la SOBRESCRIBE con el Host real de la peticion del
        # navegador (`proxyRequest.setHeader('x-forwarded-host',
        # request.headers.host)`), asi que el navegador de otra web no la
        # puede elegir: llega el host de Umbrel y su Origin no casa.
        fwd = request.headers.get("x-forwarded-host")
        if fwd and await run_in_threadpool(is_trusted_proxy, peer):
            hosts.add(fwd.split(",")[0].strip().lower())
        hosts.discard("")
        if not origin_host or origin_host not in hosts:
            raise ApiError("forbidden", "la peticion viene de otra web (Origin distinto del "
                           "servidor); el panel solo acepta su propio origen")
    return None


# ---------------- tests ----------------

def reset_state() -> None:
    """Vacia lo que se guarda en memoria (limites, fallos seguidos, pares)."""
    global _peers_cache
    _limiter.reset()
    _networks.cache_clear()
    with _peers_lock:
        _peers_cache = None
