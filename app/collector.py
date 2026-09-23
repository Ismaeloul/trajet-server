"""Recogida de andenes en segundo plano, para que la prevision aprenda sola.

Aprende de TODO el dia de servicio, no solo de la franja en la que viajo: por
la misma parada y la misma linea pasan decenas de trenes al dia y cada uno
enriquece el historico. Lo que se ajusta no es el horario sino el ritmo.

El ritmo se recalcula en cada vuelta a partir de dos cosas: cuantas llamadas
quedan hoy y cuanto falta para que se reinicie la cuota (medianoche UTC). Es
un lazo cerrado, no un plan: si me paso la tarde con la app abierta, la
pantalla gasta cuota, el recolector lo ve en la siguiente vuelta y se separa
solo. Si sobra cuota, se acerca. Y si se acaba, se calla hasta el reinicio.

    intervalo = segundos_hasta_el_reinicio * estaciones / llamadas_utiles

Dentro de la franja de una ruta se muestrea PRIORIDAD veces mas a menudo,
porque ahi si importa pillar el momento exacto en que aparece la via.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

from . import db, platform, prim
from .board import extract_departures
from .config import settings
from .idfm import norm_text, sa_to_siri

log = logging.getLogger("trajet.collector")

# Nunca mas rapido que esto. La via aparece con 7,7 min de mediana (medido el
# 30/08), asi que cada 2 min no se escapa ninguna; mas seria tirar cuota.
MIN_INTERVAL = 120
# Ni mas lento que esto mientras haya cuota: si no, no aprende nada.
MAX_INTERVAL = 1800

# Llamadas que NO se tocan nunca: son para la pantalla. Una ruta de 5 tramos
# cuesta 5 llamadas por refresco, asi que la pantalla necesita bastante mas
# de lo que yo habia calculado pensando en rutas de uno o dos tramos.
RESERVA = 320

# Cuantas veces mas a menudo se mira dentro de la franja de una ruta.
PRIORIDAD = 3
MARGIN = timedelta(minutes=30)

# Horas muertas: no circula casi nada y no hay nada que aprender.
QUIET_FROM = time(1, 0)
QUIET_TO = time(5, 0)

# Modos que no publican anden nunca: no tiene sentido sondearlos.
# Se comparan sin tildes: IDFM manda "Métro", y "Métro".lower() sigue
# llevando el acento, asi que una comparacion directa no casa.
SIN_ANDEN = {"metro", "bus", "tram", "tramway", "funicular", "funiculaire"}


def _hhmm(value: str) -> tuple[int, int]:
    try:
        h, m = value.split(":", 1)
        return int(h), int(m[:2])
    except (ValueError, AttributeError):
        return 0, 0


def _in_window(route: dict, now: datetime) -> bool:
    """La ruta esta 'en horas' ahora mismo, con margen.

    Ya no decide SI se aprende, solo con cuanta frecuencia.
    """
    if now.weekday() not in (route.get("days") or []):
        return False
    h0, m0 = _hhmm(route.get("time_from") or "00:00")
    h1, m1 = _hhmm(route.get("time_to") or "23:59")
    start = now.replace(hour=h0, minute=m0, second=0, microsecond=0) - MARGIN
    end = now.replace(hour=h1, minute=m1, second=0, microsecond=0) + MARGIN
    return start <= now <= end


def is_quiet(now: datetime) -> bool:
    """De madrugada no circula casi nada: no se gasta cuota en aprender."""
    return QUIET_FROM <= now.time() < QUIET_TO


# Cuantas estaciones se estudian como mucho a la vez. Con seis rutas
# guardadas salian nueve estaciones y el aprendizaje se comia la cuota de la
# pantalla. Se estudian las de las rutas que USO de verdad, no las que estan
# guardadas: una ruta que se probo una vez y no se ha vuelto a mirar no
# merece gastar cuota todos los dias.
MAX_ESTACIONES = 4


def targets(routes: list[dict]) -> dict[str, list[dict]]:
    """Estaciones que hay que estudiar, con los tramos que pasan por ellas.

    Se agrupa por estacion porque una sola llamada devuelve las salidas de
    todos los andenes de esa estacion: dos tramos desde la misma parada
    cuestan una llamada, no dos.
    """
    # Las rutas mas consultadas primero: son las que de verdad uso.
    try:
        uso = db.route_usage()
    except Exception:
        uso = {}
    ordenadas = sorted(routes, key=lambda r: -uso.get(r.get("id"), 0))

    out: dict[str, list[dict]] = {}
    for route in ordenadas:
        for leg in route.get("legs", []):
            if norm_text(leg.get("line_mode") or "") in SIN_ANDEN:
                continue
            if not leg.get("from_id"):
                continue
            if leg["from_id"] not in out and len(out) >= MAX_ESTACIONES:
                continue
            out.setdefault(leg["from_id"], []).append(leg)
    return out


def seconds_to_reset(now_utc: datetime | None = None) -> float:
    """Hasta la medianoche UTC, que es cuando PRIM reinicia la cuota."""
    now = now_utc or datetime.now(timezone.utc)
    manana = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max((manana - now).total_seconds(), 1.0)


def plan_interval(remaining: int | None, stations: int, now: datetime,
                  priority: bool) -> tuple[float, str]:
    """Cada cuanto muestrear, y por que. Devuelve (segundos, motivo).

    Un intervalo de 0 significa "no muestrear ahora".
    """
    if not stations:
        return 0, "ninguna ruta con tren o RER"
    if is_quiet(now):
        return 0, "horas muertas, no circula nada"
    if remaining is None:
        # Todavia no sabemos la cuota: una pasada prudente y la aprendemos
        # de las cabeceras de la respuesta.
        return 300, "cuota desconocida, ritmo prudente"

    usable = remaining - RESERVA
    if usable <= 0:
        return 0, f"cuota agotada ({remaining}), reservado para la pantalla"

    # El reloj es el que nos pasan, no el del sistema: asi el ritmo se puede
    # comprobar con una hora fija y no depende de cuando se lance el test.
    faltan = seconds_to_reset(now.astimezone(timezone.utc))
    ideal = faltan * stations / usable
    interval = max(MIN_INTERVAL, min(MAX_INTERVAL, ideal))
    if priority:
        interval = max(MIN_INTERVAL, interval / PRIORIDAD)

    motivo = f"{usable} llamadas para {faltan / 3600:.1f} h"
    if priority:
        motivo += ", en franja de ruta"
    return interval, motivo


async def sample_once(now: datetime | None = None) -> dict:
    """Una pasada. Devuelve un resumen para poder verlo desde /api/health."""
    now = now or datetime.now(settings.tz)
    routes = [db.get_route(r["id"]) for r in db.list_routes()]
    routes = [r for r in routes if r]
    plan = targets(routes)
    priority = any(_in_window(r, now) for r in routes)

    client = prim.get_client()
    # Lo que queda HOY segun el contador de cuota (dia UTC). Antes era la
    # ultima cabecera de PRIM, que solo se renueva al llamar: si ayer quedo
    # por debajo de la reserva, tras la medianoche no se volvia a llamar
    # nunca y no se aprendia hasta que alguien abria la app (fallo 18.3.8 de
    # docs/servidor.md). El contador empieza el dia solo.
    remaining = client.quota_counter.remaining("stop-monitoring")
    interval, motivo = plan_interval(remaining, len(plan), now, priority)

    base = {"stations": len(plan), "recorded": 0, "reason": motivo,
            "interval": round(interval), "remaining": remaining,
            "priority": priority}
    if not interval:
        return base

    grabadas = 0
    for stop_id, legs in plan.items():
        try:
            # Se acepta lo que ya haya en cache de los ultimos 2 min: si la
            # pantalla acaba de pedir esta estacion, aprender de su respuesta
            # sale gratis en vez de gastar otra llamada.
            payload, _ = await client.stop_monitoring(sa_to_siri(stop_id),
                                                      ttl=MIN_INTERVAL)
        except Exception as e:                # una estacion caida no para el resto
            log.warning("no se pudo muestrear %s: %s", stop_id, e)
            continue
        # Un tramo por linea: si dos rutas comparten linea y parada, no se
        # recorre dos veces lo mismo.
        vistas = set()
        for leg in legs:
            if leg["line_id"] in vistas:
                continue
            vistas.add(leg["line_id"])
            # Sin filtro de direccion: para aprender el anden interesan todos
            # los trenes de la linea, no solo los de mi sentido.
            sin_filtro = dict(leg, directions=[])
            for d in extract_departures(payload, sin_filtro, limit=60):
                if not d.get("platform"):
                    continue
                if platform.record(stop_id, leg["line_id"],
                                   d.get("destination", ""), d.get("train"),
                                   d.get("aimed_at") or d.get("at"),
                                   d["platform"], when=now):
                    grabadas += 1

    base["recorded"] = grabadas
    return base


class Collector:
    """Tarea de fondo. Se arranca y se para con la app."""

    # Cada cuanto se vuelve a mirar cuando ahora mismo no toca muestrear
    # (horas muertas, cuota agotada, o ninguna ruta con tren).
    IDLE = 600

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.last: dict = {"stations": 0, "recorded": 0, "reason": "sin arrancar",
                           "interval": 0, "remaining": None, "priority": False}
        self.last_at: str = ""
        self.total = 0

    async def _loop(self):
        # Un respiro al arrancar: que la app termine de levantarse.
        await asyncio.sleep(15)
        while True:
            espera = self.IDLE
            try:
                self.last = await sample_once()
                self.total += self.last["recorded"]
                self.last_at = datetime.now(settings.tz).strftime("%H:%M:%S")
                espera = self.last["interval"] or self.IDLE
                if self.last["recorded"]:
                    log.info("andenes aprendidos: %d (total %d), siguiente en %ds",
                             self.last["recorded"], self.total, round(espera))
            except asyncio.CancelledError:
                raise
            except Exception as e:            # nunca debe tumbar la app
                log.warning("fallo en la recogida: %s", e)
                self.last = {"stations": 0, "recorded": 0, "reason": str(e),
                             "interval": 0, "remaining": None, "priority": False}
            await asyncio.sleep(espera)

    def start(self):
        if settings.collect and self.task is None:
            self.task = asyncio.create_task(self._loop())
            log.info("recolector de andenes en marcha")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    def status(self) -> dict:
        return {"enabled": settings.collect, "running": self.task is not None,
                "last_at": self.last_at, "session_total": self.total, **self.last}


collector = Collector()
