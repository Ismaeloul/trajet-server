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


def _parse(ts) -> datetime | None:
    """Hora ISO de SIRI. Una hora sin zona se toma como UTC (SIRI siempre
    manda UTC) para no comparar una hora con zona con otra sin ella."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# SIRI no siempre manda lo que promete: un campo puede llegar null, suelto en
# vez de en lista o con otro tipo. Estas tres ayudas hacen que un dato raro
# degrade (ese campo vacio, esa visita fuera) en vez de tumbar el tablero con
# un 500 (fallo 18.3.18 de docs/servidor.md: `TrainNumbers: null`).

def _obj(value) -> dict:
    """El objeto de un campo SIRI, o {} si no es un objeto."""
    return value if isinstance(value, dict) else {}


def _objs(value) -> list[dict]:
    """Los objetos de un campo SIRI que deberia ser una lista de objetos."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


def _text(value) -> str:
    """Texto de un valor SIRI ya desenvuelto (first_value), o "" si no hay."""
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


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


_DIA = 24 * 60


def _window_min(route: dict) -> tuple[int, int]:
    """Franja de la ruta en minutos desde medianoche."""
    return (_to_min(route.get("time_from") or "00:00"),
            _to_min(route.get("time_to") or "23:59", _DIA - 1))


def _width(route: dict) -> int:
    """Minutos que abarca la franja (una que cruza medianoche tambien)."""
    ini, fin = _window_min(route)
    return (fin - ini) % _DIA


def _covers(route: dict, weekday: int, minute: int) -> bool:
    """La ruta esta en su franja en ese dia y minuto.

    Admite franjas que cruzan medianoche (23:00-01:00). La parte de despues de
    medianoche pertenece al dia en que empezo: la vuelta del viernes por la
    noche sigue siendo del viernes a la 00:30 del sabado.
    """
    days = route.get("days") or []
    ini, fin = _window_min(route)
    if ini <= fin:
        return weekday in days and ini <= minute <= fin
    if minute >= ini:
        return weekday in days
    if minute <= fin:
        return (weekday - 1) % 7 in days
    return False


def pick_active_route(routes: list[dict], when: datetime | None = None) -> dict | None:
    """Que ruta toca ahora mismo, segun dia de la semana y franja horaria (R37).

    1. Si alguna esta en su franja, gana la mas concreta.
    2. Si no, la proxima de hoy DE VERDAD: la que empieza antes a partir de
       ahora, no la primera de la lista (en la 0.3.0 se cogia la primera de la
       lista con la franja por delante, aunque hubiera otra mas cercana).
    3. Si hoy ya no queda ninguna, la que acabo hace menos.
    4. Si hoy no hay ninguna, la primera de la lista.

    Nunca deja la pantalla vacia. Las horas se comparan en minutos, no como
    cadenas: asi funcionan las franjas que cruzan medianoche.
    """
    if not routes:
        return None
    when = when or now_paris()
    weekday = when.weekday()          # 0 = lunes
    minute = when.hour * 60 + when.minute

    def orden(r: dict) -> tuple:
        return (r.get("position", 0) or 0, r.get("id", 0) or 0)

    # Si encajan varias, gana la mas concreta: una ruta de "llego a las 09:00"
    # abarca dos horas, y una franja de "07:00 a 22:00" abarca el dia entero.
    # Sin esto, la generica tapaba a la buena solo por estar antes en la lista.
    encajan = [r for r in routes if _covers(r, weekday, minute)]
    if encajan:
        return min(encajan, key=lambda r: (_width(r), *orden(r)))

    today = [r for r in routes if weekday in (r.get("days") or [])]
    if today:
        por_delante = [r for r in today if _window_min(r)[0] > minute]
        if por_delante:
            return min(por_delante, key=lambda r: (_window_min(r)[0] - minute,
                                                   _width(r), *orden(r)))
        return min(today, key=lambda r: ((minute - _window_min(r)[1]) % _DIA, *orden(r)))
    return routes[0]


# ---------------- perturbaciones ----------------

def index_disruptions(payload: dict, now: datetime | None = None) -> dict[str, list[dict]]:
    """Agrupa los avisos activos por codigo de linea.

    `now` (UTC) se puede fijar para probar; por defecto, ahora. El "hoy" con
    el que se decide si un aviso es de obras futuras es la fecha de PARIS: con
    la fecha UTC, entre las 00:00 y las 02:00 de Paris un aviso para hoy
    salia como planificado (fallo 18.3.19).
    """
    out: dict[str, list[dict]] = {}
    try:
        deliveries = payload["Siri"]["ServiceDelivery"]["GeneralMessageDelivery"]
    except (KeyError, TypeError):
        return out

    now = now or datetime.now(timezone.utc)
    hoy = now.astimezone(settings.tz).date()
    for delivery in _objs(deliveries):
        for msg in _objs(delivery.get("InfoMessage")):
            valid_until = _parse(msg.get("ValidUntilTime"))
            if valid_until and valid_until < now:
                continue
            texts = []
            for m in _objs(_obj(msg.get("Content")).get("Message")):
                t = _text(first_value(m.get("MessageText")))
                if t and t not in texts:
                    texts.append(t)
            if not texts:
                continue
            # El SHORT_MESSAGE suele ser el mas legible; si no, el primero
            text = min(texts, key=len)

            # Un aviso de obras para dentro de un mes NO es una perturbacion
            # de hoy. Sin este filtro casi todas las lineas salian en rojo.
            planned_from = starts_later(" ".join(texts), hoy)

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

def extract_departures(payload: dict, leg: dict, limit: int = 4,
                       remember: bool = True) -> list[dict]:
    """Saca las proximas salidas de la linea y direccion de este tramo.

    `remember=False` no toca la memoria de «via recien aparecida»: el
    recolector lee las mismas estaciones y, si la apuntase el, el tablero ya
    no veria aparecer la via y no la cantaria (fallo 18.3.11).
    """
    want_line = line_code(leg["line_id"])
    wanted_dirs = {norm_text(d) for d in (leg.get("directions") or [])
                   if isinstance(d, str) and d.strip()}

    try:
        deliveries = payload["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"]
    except (KeyError, TypeError):
        return []

    now = datetime.now(timezone.utc)
    rows = []
    for delivery in _objs(deliveries):
        for visit in _objs(delivery.get("MonitoredStopVisit")):
            row = _departure(visit, want_line, wanted_dirs, now, remember)
            if row is not None:
                rows.append(row)

    rows.sort(key=lambda r: r["minutes"])
    return rows[:limit]


def _departure(visit: dict, want_line: str, wanted_dirs: set[str], now: datetime,
               remember: bool) -> dict | None:
    """Una visita de SIRI convertida en salida, o None si no es de este tramo."""
    mvj = _obj(visit.get("MonitoredVehicleJourney"))
    if line_code(_text(first_value(mvj.get("LineRef")))) != want_line:
        return None

    mc = _obj(mvj.get("MonitoredCall"))
    dest = (_text(first_value(mvj.get("DestinationName")))
            or _text(first_value(mc.get("DestinationDisplay"))))
    if wanted_dirs and norm_text(dest) not in wanted_dirs:
        return None

    expected = _parse(mc.get("ExpectedDepartureTime")
                      or mc.get("ExpectedArrivalTime"))
    aimed = _parse(mc.get("AimedDepartureTime")
                   or mc.get("AimedArrivalTime"))
    if not expected:
        return None
    minutes = (expected - now).total_seconds() / 60
    if minutes < -1:          # ya se ha ido
        return None

    # El retraso solo se puede calcular si viene la hora teorica.
    # En bus casi nunca viene: en ese caso nos fiamos del estado.
    delay = None
    if aimed:
        delay = round((expected - aimed).total_seconds() / 60)

    jid = (_text(first_value(_obj(mvj.get("FramedVehicleJourneyRef"))
                             .get("DatedVehicleJourneyRef")))
           or _text(first_value(visit.get("ItemIdentifier"))))
    platform = real_platform(mc.get("DeparturePlatformName"))

    # Aparece un anden donde antes no habia? Eso hay que cantarlo.
    is_new = False
    if jid and remember:
        before = _seen_platforms.get(jid)
        if platform and before != platform:
            is_new = before is None or before == ""
            _remember_platform(jid, platform)
        elif platform is None and jid not in _seen_platforms:
            _remember_platform(jid, "")

    at_stop = mc.get("VehicleAtStop")
    return {
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
        "status": _text(mc.get("DepartureStatus")) or _text(mc.get("ArrivalStatus")),
        "at_stop": at_stop is True or _text(at_stop).lower() == "true",
        "train": _text(first_value(_obj(mvj.get("TrainNumbers")).get("TrainNumberRef"))) or None,
        # La API no da ocupacion (comprobado: 211 salidas, ni un solo
        # campo). Lo que si da, y en el 61 % de las salidas, es si el
        # tren es corto o largo. En un tren corto vas mas apretado y
        # ademas para en otra parte del anden, asi que es informacion
        # que se usa de verdad.
        "length": train_length(mvj.get("VehicleFeatureRef")),
    }


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
    for delivery in _objs(deliveries):
        for visit in _objs(delivery.get("MonitoredStopVisit")):
            mvj = _obj(visit.get("MonitoredVehicleJourney"))
            if line_code(_text(first_value(mvj.get("LineRef")))) != want:
                continue
            dest = (_text(first_value(mvj.get("DestinationName")))
                    or _text(first_value(_obj(mvj.get("MonitoredCall")).get("DestinationDisplay"))))
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
