"""Logica de negocio: construir el tablero de la ruta activa.

Aqui vive todo lo que traduce las respuestas crudas de PRIM en las cuatro
cifras que quiero ver de pie en un anden: cuantos minutos faltan, a que
destino va, por que via sale y cuanto lleva de retraso.
"""
import asyncio
import logging
import re
from datetime import datetime, timezone

from . import db
from .config import settings
from .frdate import starts_later
from .idfm import (first_value, line_code, lines_in_message, norm_text,
                   publishes_platform, real_platform, sa_to_siri)
from .prim import get_client

log = logging.getLogger("trajet.board")

# Palabras que en los avisos de IDFM significan que la linea esta cortada,
# no solo con retrasos. Los textos vienen siempre en frances.
# OJO: IDFM escribe "le trafic EST interrompu", con la copula en medio. Sin
# admitirla, la linea 13 cortada de verdad el 30/08 salia solo como
# "perturbada". El verbo puede ir tambien en plural o en futuro.
_INTERRUPTED = re.compile(
    r"trafic\s+(?:est|sera|reste|sont|seront)?\s*interrompu"
    r"|interruption\s+(?:de\s+|du\s+)?trafic"
    r"|ligne\s+(?:est\s+)?interrompue"
    r"|interrompu\w*\s+sur\s+(?:toute\s+)?la\s+ligne"
    r"|ne\s+circule(?:nt)?\s+(?:plus|pas)|aucun\s+train"
    r"|service\s+(?:est\s+)?interrompu",
    re.IGNORECASE)

NORMAL, DISRUPTED, INTERRUPTED = 0, 1, 2

# Memoria de andenes ya vistos, para detectar cuando aparece uno nuevo.
# Clave: identificador del viaje. Solo en memoria: si se reinicia el
# contenedor se pierde, y lo unico que pasa es que no parpadea una vez.
#
# Los identificadores llevan la fecha dentro, asi que cada dia son nuevos y el
# diccionario creceria sin parar. Se limita a los ultimos N: un tablero enseña
# 4 pasos por tramo, y el sondeo del 30/08 vio como mucho 198 salidas por
# estacion en una muestra, asi que 4000 sobran de largo para no perder ninguna
# transicion "sin anden -> anden" en curso.
_SEEN_MAX = 4000
_seen_platforms: dict[str, str] = {}


def _remember_platform(jid: str, platform: str) -> None:
    """Apunta el anden de un viaje, tirando los mas antiguos si sobra memoria."""
    _seen_platforms[jid] = platform
    if len(_seen_platforms) > _SEEN_MAX:
        for old_jid in list(_seen_platforms)[: len(_seen_platforms) - _SEEN_MAX]:
            del _seen_platforms[old_jid]


def train_length(feature) -> str | None:
    """'short' o 'long' a partir de VehicleFeatureRef, o None si no viene."""
    v = norm_text(first_value(feature) or "")
    if "shorttrain" in v.replace(" ", ""):
        return "short"
    if "longtrain" in v.replace(" ", ""):
        return "long"
    return None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_paris() -> datetime:
    return datetime.now(settings.tz)


# Cuanto se supone que dura un trayecto cuando no lo sabemos (rutas montadas
# a mano). Solo sirve para calcular la franja en que la ruta esta "en horas".
DEFAULT_TRIP = 60

# Margen antes de salir (ya estoy mirando la app) y despues de llegar.
ANTES = 45
DESPUES = 30


def _to_min(hhmm: str, fallback: int = 0) -> int:
    try:
        h, m = str(hhmm).split(":", 1)
        return int(h) * 60 + int(m[:2])
    except (ValueError, AttributeError):
        return fallback


def _to_hhmm(minutes: int) -> str:
    minutes = max(0, min(24 * 60 - 1, minutes))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def derive_window(route: dict) -> tuple[str, str]:
    """Franja horaria de una ruta segun como se haya definido.

    Se puede decir de tres formas: una franja de toda la vida, "salgo a las
    08:00" o "quiero llegar a las 09:00". Las dos ultimas se traducen aqui a
    una franja, asi que todo lo que ya funcionaba (que ruta toca ahora, la
    prioridad del recolector) sigue igual sin enterarse.
    """
    mode = route.get("time_mode") or "window"
    at = route.get("time_at") or ""
    if mode not in ("departure", "arrival") or not at:
        return (route.get("time_from") or "07:00",
                route.get("time_to") or "10:00")

    dur = int(route.get("duration_min") or 0) or DEFAULT_TRIP
    t = _to_min(at)
    if mode == "departure":
        return _to_hhmm(t - ANTES), _to_hhmm(t + dur + DESPUES)
    # llegada: la franja va hacia atras desde la hora a la que quiero estar
    return _to_hhmm(t - dur - ANTES), _to_hhmm(t + DESPUES // 2)


def pick_active_route(routes: list[dict], when: datetime | None = None) -> dict | None:
    """Que ruta toca ahora mismo, segun dia de la semana y franja horaria.

    Si ninguna encaja exactamente, devuelve la primera del dia de hoy, y si
    tampoco hay, la primera de la lista. Nunca deja la pantalla vacia.
    """
    if not routes:
        return None
    when = when or now_paris()
    weekday = when.weekday()          # 0 = lunes
    hhmm = when.strftime("%H:%M")

    today = [r for r in routes if weekday in r["days"]]

    # Si encajan varias, gana la mas concreta: una ruta de "llego a las 09:00"
    # abarca dos horas, y una franja de "07:00 a 22:00" abarca el dia entero.
    # Sin esto, la generica tapaba a la buena solo por estar antes en la lista.
    encajan = [r for r in today if r["time_from"] <= hhmm <= r["time_to"]]
    if encajan:
        return min(encajan, key=lambda r: (
            _to_min(r["time_to"], 24 * 60) - _to_min(r["time_from"]),
            r.get("position", 0), r["id"]))
    if today:
        # Nada en franja: la siguiente que venga hoy, si no la ultima
        upcoming = [r for r in today if r["time_from"] >= hhmm]
        return upcoming[0] if upcoming else today[-1]
    return routes[0]


# ---------------- perturbaciones ----------------

def index_disruptions(payload: dict) -> dict[str, list[dict]]:
    """Agrupa los avisos activos por codigo de linea."""
    out: dict[str, list[dict]] = {}
    try:
        deliveries = payload["Siri"]["ServiceDelivery"]["GeneralMessageDelivery"]
    except (KeyError, TypeError):
        return out

    now = datetime.now(timezone.utc)
    for delivery in deliveries:
        for msg in delivery.get("InfoMessage", []) or []:
            valid_until = _parse(msg.get("ValidUntilTime"))
            if valid_until and valid_until < now:
                continue
            texts = []
            for m in msg.get("Content", {}).get("Message", []) or []:
                t = (m.get("MessageText") or {}).get("value")
                if t and t not in texts:
                    texts.append(t)
            if not texts:
                continue
            # El SHORT_MESSAGE suele ser el mas legible; si no, el primero
            text = min(texts, key=len)

            # Un aviso de obras para dentro de un mes NO es una perturbacion
            # de hoy. Sin este filtro casi todas las lineas salian en rojo.
            planned_from = starts_later(" ".join(texts), now.date())

            entry = {
                "text": text,
                "severity": INTERRUPTED if _INTERRUPTED.search(" ".join(texts)) else DISRUPTED,
                "until": msg.get("ValidUntilTime"),
                "planned_from": planned_from.isoformat() if planned_from else None,
            }
            for code in lines_in_message(msg):
                out.setdefault(code, []).append(entry)
    return out


def line_status(code: str, disruptions: dict[str, list[dict]]) -> dict:
    """Estado de una linea, contando solo lo que pasa HOY.

    Los avisos con fecha de inicio futura se apartan en 'planned': se
    conservan por si algun dia quiero verlos, pero no encienden el semaforo.
    """
    msgs = disruptions.get(code, [])
    active = [m for m in msgs if not m["planned_from"]]
    planned = [m for m in msgs if m["planned_from"]]

    if not active:
        return {"level": NORMAL, "label": "normal", "messages": [],
                "planned": len(planned)}
    level = max(m["severity"] for m in active)
    return {
        "level": level,
        "label": "interrumpida" if level == INTERRUPTED else "perturbada",
        "messages": [m["text"] for m in active[:3]],
        "planned": len(planned),
    }


# ---------------- proximos pasos ----------------

def extract_departures(payload: dict, leg: dict, limit: int = 4) -> list[dict]:
    """Saca las proximas salidas de la linea y direccion de este tramo."""
    want_line = line_code(leg["line_id"])
    wanted_dirs = {norm_text(d) for d in (leg.get("directions") or []) if d.strip()}

    try:
        deliveries = payload["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"]
    except (KeyError, TypeError):
        return []

    now = datetime.now(timezone.utc)
    rows = []
    for delivery in deliveries:
        for visit in delivery.get("MonitoredStopVisit", []) or []:
            mvj = visit.get("MonitoredVehicleJourney", {})
            if line_code(first_value(mvj.get("LineRef")) or "") != want_line:
                continue

            mc = mvj.get("MonitoredCall", {})
            dest = (first_value(mvj.get("DestinationName"))
                    or first_value(mc.get("DestinationDisplay")) or "")
            if wanted_dirs and norm_text(dest) not in wanted_dirs:
                continue

            expected = _parse(mc.get("ExpectedDepartureTime")
                              or mc.get("ExpectedArrivalTime"))
            aimed = _parse(mc.get("AimedDepartureTime")
                           or mc.get("AimedArrivalTime"))
            if not expected:
                continue
            minutes = (expected - now).total_seconds() / 60
            if minutes < -1:          # ya se ha ido
                continue

            # El retraso solo se puede calcular si viene la hora teorica.
            # En bus casi nunca viene: en ese caso nos fiamos del estado.
            delay = None
            if aimed:
                delay = round((expected - aimed).total_seconds() / 60)

            jid = ((mvj.get("FramedVehicleJourneyRef") or {})
                   .get("DatedVehicleJourneyRef")
                   or visit.get("ItemIdentifier") or "")
            platform = real_platform(mc.get("DeparturePlatformName"))

            # Aparece un anden donde antes no habia? Eso hay que cantarlo.
            is_new = False
            if jid:
                before = _seen_platforms.get(jid)
                if platform and before != platform:
                    is_new = before is None or before == ""
                    _remember_platform(jid, platform)
                elif platform is None and jid not in _seen_platforms:
                    _remember_platform(jid, "")

            rows.append({
                "jid": jid,
                "minutes": int(max(0, round(minutes))),
                "at": expected.astimezone(settings.tz).strftime("%H:%M"),
                # La hora TEORICA es la que sirve para aprender el anden: la
                # prevista se mueve con el retraso y no identifica al tren.
                "aimed_at": (aimed.astimezone(settings.tz).strftime("%H:%M")
                             if aimed else ""),
                "destination": dest,
                "platform": platform,
                "platform_new": is_new,
                "delay": delay,
                "status": mc.get("DepartureStatus") or mc.get("ArrivalStatus") or "",
                "at_stop": bool(mc.get("VehicleAtStop")),
                "train": first_value(mvj.get("TrainNumbers", {}).get("TrainNumberRef")),
                # La API no da ocupacion (comprobado: 211 salidas, ni un solo
                # campo). Lo que si da, y en el 61 % de las salidas, es si el
                # tren es corto o largo. En un tren corto vas mas apretado y
                # ademas para en otra parte del anden, asi que es informacion
                # que se usa de verdad.
                "length": train_length(mvj.get("VehicleFeatureRef")),
            })

    rows.sort(key=lambda r: r["minutes"])
    return rows[:limit]


def observed_destinations(payload: dict, navitia_line_id: str) -> list[str]:
    """Destinos que se ven ahora mismo para una linea en una parada.

    Se usa al dar de alta un tramo: en vez de pedirle al usuario que escriba
    la direccion, le enseno las que realmente circulan y elige.
    """
    want = line_code(navitia_line_id)
    seen: dict[str, int] = {}
    try:
        deliveries = payload["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"]
    except (KeyError, TypeError):
        return []
    for delivery in deliveries:
        for visit in delivery.get("MonitoredStopVisit", []) or []:
            mvj = visit.get("MonitoredVehicleJourney", {})
            if line_code(first_value(mvj.get("LineRef")) or "") != want:
                continue
            dest = (first_value(mvj.get("DestinationName"))
                    or first_value(mvj.get("MonitoredCall", {}).get("DestinationDisplay")))
            if dest:
                seen[dest] = seen.get(dest, 0) + 1
    return [d for d, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


# ---------------- ritmo de refresco ----------------

# Cuanto falta para el proximo paso de cada estacion, de la ultima vez que se
# miro. Solo en memoria: si se reinicia, se vuelve al ritmo rapido y ya.
_next_in: dict[str, float] = {}

# Una ruta de 5 tramos son 5 llamadas por refresco. A 30 s eso son 600
# llamadas/hora y la cuota diaria entera es de 1000: con la app abierta te
# quedas sin cuota en hora y media. Pero de esas 5 estaciones, las de los
# ultimos tramos salen dentro de media hora y su dato no cambia en nada util.
# Asi que cada estacion se refresca al ritmo de lo cerca que este SU proximo
# paso, no al ritmo de la pantalla.
TTL_POR_CERCANIA = (
    (3,   20),      # sale en 3 min o menos: al segundo
    (8,   30),
    (20,  90),
    (45,  300),
)
TTL_LEJOS = 600     # mas de 45 min: cada 10 minutos sobra


def station_ttl(stop_id: str) -> int:
    """Cada cuanto merece la pena volver a pedir esta estacion."""
    faltan = _next_in.get(stop_id)
    if faltan is None:
        return settings.ttl_stop_monitoring     # primera vez: ritmo normal
    for limite, ttl in TTL_POR_CERCANIA:
        if faltan <= limite:
            return ttl
    return TTL_LEJOS


def remember_next(stop_id: str, deps: list[dict]) -> None:
    """Apunta a cuanto esta el proximo paso, para el ritmo de la proxima vez."""
    if deps:
        _next_in[stop_id] = min(d["minutes"] for d in deps)
    else:
        _next_in.pop(stop_id, None)


# ---------------- tablero completo ----------------

async def build_board(route: dict) -> dict:
    """Junta todo: por cada tramo, proximos pasos y estado de la linea.

    Devuelve el tablero de la 0.3.0 mas lo que necesita la v1 (`from_id`,
    `to_id` y `platform_expected` en cada tramo, y `disruptions_ok`) y dos
    claves internas que empiezan por `_` y que las capas de la API quitan
    antes de responder:

      _all_failed   ninguna estacion se pudo leer (ni en cache): el tablero
                    estaria hueco y la v1 responde con error para que la app
                    se quede con su ultimo tablero bueno (regla 9).
      _error        la PrimError de la primera estacion que fallo.
    """
    prim = get_client()

    # Una sola llamada por ESTACION, aunque varios tramos salgan de la misma.
    stations = list(dict.fromkeys(leg["from_id"] for leg in route["legs"]))
    ttls = {s: station_ttl(s) for s in stations}
    tasks = [prim.stop_monitoring(sa_to_siri(s), ttls[s]) for s in stations]
    tasks.append(prim.general_message())
    results = await asyncio.gather(*tasks, return_exceptions=True)

    monitoring: dict[str, tuple] = {}
    for station, res in zip(stations, results[:-1]):
        monitoring[station] = res

    gm = results[-1]
    disruptions: dict[str, list[dict]] = {}
    gm_age = None
    errors: list[str] = []
    if isinstance(gm, Exception):
        # Sin avisos, «normal» no es de fiar: se dice (en la 0.3.0 una linea
        # cortada salia normal en silencio).
        errors.append(f"avisos: {gm}")
    else:
        disruptions = index_disruptions(gm[0])
        gm_age = gm[1]

    legs_out = []
    worst_level = NORMAL
    worst_line = ""
    max_delay = 0.0
    stale = False
    failed: dict[str, Exception] = {}
    ages: list[float] = []
    next_by_station: dict[str, list[dict]] = {}

    for leg in route["legs"]:
        res = monitoring.get(leg["from_id"])
        deps: list[dict] = []
        age = None
        if isinstance(res, Exception):
            if leg["from_id"] not in failed:
                failed[leg["from_id"]] = res
                errors.append(f"{leg['from_name']}: {res}")
            stale = True
        elif res is not None:
            payload, age = res
            deps = extract_departures(payload, leg)
            next_by_station.setdefault(leg["from_id"], []).extend(deps)
            ages.append(age or 0)
            # Viejo = mas antiguo de lo que su propio ritmo de refresco
            # permite (con un minuto de margen), no un umbral fijo: con el TTL
            # adaptativo una estacion con el tren a 40 min se pide cada 5 min
            # y eso no es un dato viejo.
            # (Con la cuota justa el cliente alarga el TTL: eso tampoco es viejo.)
            ttl = prim.effective_ttl(ttls.get(leg["from_id"], settings.ttl_stop_monitoring),
                                     "stop-monitoring")
            if (age or 0) > ttl + 60:
                stale = True

        code = line_code(leg["line_id"])
        status = line_status(code, disruptions)
        if status["level"] > worst_level:
            worst_level = status["level"]
            worst_line = leg["line_code"] or code

        for d in deps:
            if d["delay"]:
                if d["delay"] > max_delay:
                    max_delay = float(d["delay"])
                    if worst_level == NORMAL:
                        worst_line = leg["line_code"] or code

        legs_out.append({
            "seq": leg["seq"],
            "line_id": leg["line_id"],
            "line_code": leg["line_code"],
            "line_name": leg["line_name"],
            "line_mode": leg["line_mode"],
            "line_color": leg["line_color"],
            "from_name": leg["from_name"],
            "to_name": leg["to_name"],
            "directions": leg.get("directions") or [],
            "status": status,
            "departures": deps,
            "age": round(age, 1) if age is not None else None,
            # v1
            "from_id": leg["from_id"],
            "to_id": leg.get("to_id") or "",
            "platform_expected": publishes_platform(leg.get("line_mode") or ""),
        })

    # El ritmo de cada estacion lo marca su paso mas cercano de TODOS sus
    # tramos (en la 0.3.0 mandaba el ultimo tramo leido).
    for station in stations:
        if station in next_by_station:
            remember_next(station, next_by_station[station])
        elif station not in failed:
            remember_next(station, [])

    if gm_age is not None:
        ages.append(gm_age)
        if gm_age > prim.effective_ttl(settings.ttl_general_message, "general-message") + 60:
            stale = True

    data_age = round(max(ages), 1) if ages else 0.0
    return {
        "route": {
            "id": route["id"], "name": route["name"],
            "origin_name": route["origin_name"], "dest_name": route["dest_name"],
        },
        "legs": legs_out,
        "worst_level": worst_level,
        "worst_line": worst_line,
        "max_delay": max_delay,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_age": data_age,
        "stale": stale,
        "errors": errors,
        "quota": dict(prim.quota),
        "last_error": prim.last_error,
        "disruptions_ok": not isinstance(gm, Exception),
        "_all_failed": bool(stations) and len(failed) == len(stations),
        "_error": next(iter(failed.values()), None),
    }


def record_history(route: dict, board: dict, min_gap_seconds: int = 600) -> bool:
    """Guarda una observacion, sin duplicar si acabo de guardar otra."""
    last = db.last_observation_ts(route["id"])
    now = datetime.now(timezone.utc)
    if last:
        prev = _parse(last)
        if prev and (now - prev).total_seconds() < min_gap_seconds:
            return False

    detail = {
        "legs": [
            {
                "line": l["line_code"],
                "status": l["status"]["label"],
                "next": [d["minutes"] for d in l["departures"][:3]],
                "platforms": [d["platform"] for d in l["departures"][:3]],
            }
            for l in board["legs"]
        ],
    }
    db.log_observation(
        route_id=route["id"],
        ts_iso=now.isoformat(timespec="seconds"),
        day=now.astimezone(settings.tz).strftime("%Y-%m-%d"),
        disrupted=board["worst_level"],
        delay_min=board["max_delay"],
        worst_line=board["worst_line"],
        detail=detail,
    )
    return True
