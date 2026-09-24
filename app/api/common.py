"""Logica compartida por las rutas de la 0.3.0 (/api) y las de la v1.

Aqui no hay nada de HTTP: cada funcion devuelve datos o lanza ApiError con
un codigo estable. legacy.py lo traduce a `{"detail": ...}` como siempre y
v1.py a ErrorV1.
"""
from __future__ import annotations

import logging
import math
import re
import time

from fastapi.concurrency import run_in_threadpool

from .. import board as B
from .. import db, mapdata, planner, prim
from .. import platform as P
from .. import translate as T
from ..collector import collector
from ..idfm import line_code, mode_sort_key, sa_to_siri
from ..prim import PrimError
from .errors import ApiError

log = logging.getLogger("trajet.api")

# Claves que el tablero trae para la v1 y que la 0.3.0 no tenia.
_V1_LEG_KEYS = ("from_id", "to_id", "platform_expected")
_V1_BOARD_KEYS = ("disruptions_ok",)


def strip_private(data: dict) -> dict:
    """Quita las claves internas (las que empiezan por `_`)."""
    for k in [k for k in data if k.startswith("_")]:
        data.pop(k, None)
    return data


def to_legacy_board(data: dict) -> dict:
    """El tablero con la forma exacta de la 0.3.0."""
    strip_private(data)
    for k in _V1_BOARD_KEYS:
        data.pop(k, None)
    for leg in data.get("legs", []):
        for k in _V1_LEG_KEYS:
            leg.pop(k, None)
    return data


def prim_error(e: PrimError, what: str = "la API de IDFM") -> ApiError:
    """Traduce un fallo de PRIM a un ApiError con codigo estable.

    El codigo y el estado son los de la v1. Lleva ademas la respuesta de la
    0.3.0 (`legacy`): alli cualquier fallo de PRIM era 502 con «<quien> no
    responde: <motivo>», y los clientes de entonces solo conocen eso."""
    legacy = (502, f"{what} no responde: {e}")
    kind = getattr(e, "kind", "") or "http"
    if kind == "no_key":
        return ApiError("prim_key_missing", "el servidor no tiene clave de PRIM configurada",
                        legacy=legacy)
    if kind in ("invalid", "forbidden"):
        return ApiError("prim_key_invalid", "PRIM rechaza la clave del servidor", legacy=legacy)
    if kind == "quota":
        return ApiError("prim_quota_exhausted",
                        "cuota diaria de PRIM agotada; vuelve a haber a medianoche UTC",
                        legacy=legacy)
    if kind == "unreachable":
        return ApiError("prim_unreachable", f"{what} no responde", legacy=legacy)
    return ApiError("upstream", f"{what} no responde: {e}", status=502, legacy=legacy)


# ---------------- validacion ----------------

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_HEX3 = re.compile(r"[0-9A-Fa-f]{3}")
_HEX6 = re.compile(r"[0-9A-Fa-f]{6}")

# Textos opcionales de un tramo. Son columnas NOT NULL de `legs`: un null que
# llegara hasta SQLite era un IntegrityError y un 500 (H4 de la verificacion).
# Una app que serialice un opcional como null es lo normal, asi que null
# cuenta como ausente ("").
_LEG_TEXT = ("line_code", "line_name", "line_mode", "line_color", "from_name",
             "to_id", "to_name")
_LEG_COORDS = ("from_lat", "from_lon", "to_lat", "to_lon")

# Rangos razonables: una duracion de mas de un dia o una posicion enorme no
# son un trayecto, y un entero de 2^70 era un OverflowError de SQLite (500).
_NUM_RANGE = {"duration_min": (0, 1440), "position": (-10000, 10000)}


def normalize_color(value) -> str:
    """R12: color de linea como hex de 6 cifras, sin `#` y en mayusculas
    («#cec73d» -> «CEC73D», «F0A» -> «FF00AA»), o "" si no es un color."""
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if v.startswith("#"):
        v = v[1:]
    if _HEX3.fullmatch(v):
        v = "".join(c * 2 for c in v)
    return v.upper() if _HEX6.fullmatch(v) else ""


def _number(value) -> bool:
    """Numero de verdad y finito. bool no cuenta (True es un int en Python) y
    NaN/Infinity, que json.loads acepta, tampoco."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _validate_leg(i: int, leg) -> dict:
    """Un tramo ya comprobado y normalizado (copia: no toca lo recibido)."""
    if not isinstance(leg, dict):
        raise ApiError("bad_request", f"tramo {i + 1}: tiene que ser un objeto")
    out = dict(leg)
    for field in ("line_id", "from_id"):
        v = leg.get(field)
        if not isinstance(v, str) or not v.strip():
            raise ApiError("bad_request", f"tramo {i + 1}: falta '{field}'")
    for field in _LEG_TEXT:
        v = leg.get(field)
        if v is None:
            out[field] = ""
        elif not isinstance(v, str):
            raise ApiError("bad_request", f"tramo {i + 1}: '{field}' tiene que ser texto")
    out["line_color"] = normalize_color(out["line_color"])
    dirs = leg.get("directions")
    if dirs is None:
        # Sin sentido = todos los pasos. Un null guardado tal cual salia
        # luego como `directions: null` y el contrato dice lista.
        out["directions"] = []
    elif not (isinstance(dirs, list) and all(isinstance(d, str) for d in dirs)):
        raise ApiError("bad_request", f"tramo {i + 1}: 'directions' tiene que ser una lista de textos")
    for field in _LEG_COORDS:
        # Coordenadas para el mapa: opcionales; lo que no sea un numero
        # finito se descarta (el mapa tira entonces de los datos abiertos).
        if field in out and not _number(out[field]):
            out.pop(field)
    return out


def validate_route(p) -> dict:
    """Comprueba una ruta antes de guardarla. 400 con motivo, nunca 500.

    Devuelve una copia normalizada: textos opcionales null -> "", directions
    null -> [], line_color en hex de 6 cifras, numeros como enteros."""
    if not isinstance(p, dict):
        raise ApiError("bad_request", "la ruta tiene que ser un objeto JSON")
    for field in ("name", "origin_id", "dest_id"):
        if not isinstance(p.get(field, ""), str) or not str(p.get(field, "")).strip():
            raise ApiError("bad_request", f"falta el campo '{field}'")
    out = dict(p)
    for field in ("origin_name", "dest_name"):
        # Los nombres se pueden omitir (o mandar null): se guardan vacios en
        # vez de dar un 500.
        v = p.get(field)
        if v is None:
            out[field] = ""
        elif not isinstance(v, str):
            raise ApiError("bad_request", f"'{field}' tiene que ser texto")
    legs = p.get("legs")
    if not legs or not isinstance(legs, list):
        raise ApiError("bad_request", "la ruta necesita al menos un tramo")
    out["legs"] = [_validate_leg(i, leg) for i, leg in enumerate(legs)]
    days = p.get("days", [0, 1, 2, 3, 4])
    if not isinstance(days, list) or not all(
            isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6 for d in days):
        raise ApiError("bad_request", "'days' tiene que ser una lista de 0 (lunes) a 6 (domingo)")
    for field in ("time_from", "time_to", "time_at"):
        v = p.get(field)
        if v not in (None, "") and not (isinstance(v, str) and _HHMM.match(v)):
            raise ApiError("bad_request", f"'{field}' tiene que ser HH:MM")
    for field, (lo, hi) in _NUM_RANGE.items():
        v = p.get(field)
        if v is None:
            out[field] = 0
        elif not _number(v) or not lo <= v <= hi:
            raise ApiError("bad_request", f"'{field}' tiene que ser un numero de {lo} a {hi}")
        else:
            out[field] = int(v)
    return out


# ---------------- rutas ----------------

def list_routes() -> dict:
    routes = db.list_routes()
    active = B.pick_active_route(routes)
    return {"routes": routes, "active_id": active["id"] if active else None}


def create_route(payload) -> dict:
    data = validate_route(payload)
    rid = db.save_route(data)
    mapdata.schedule_route(rid)          # el mapa se calcula en segundo plano
    return {"id": rid, "route": db.get_route(rid)}


def update_route(route_id: int, payload) -> dict:
    if not db.get_route(route_id):
        raise ApiError("not_found", "ruta no encontrada")
    data = validate_route(payload)
    db.save_route(data, route_id)
    _baseline.pop(route_id, None)
    mapdata.schedule_route(route_id)
    return {"id": route_id, "route": db.get_route(route_id)}


def delete_route(route_id: int) -> dict:
    if not db.delete_route(route_id):
        raise ApiError("not_found", "ruta no encontrada")
    _baseline.pop(route_id, None)
    return {"deleted": route_id}


def get_route(route_id: int) -> dict:
    route = db.get_route(route_id)
    if not route:
        raise ApiError("not_found", "ruta no encontrada")
    return route


# ---------------- buscador ----------------

async def search_stops(q: str) -> dict:
    try:
        data, age = await prim.get_client().places(q)
    except PrimError as e:
        raise prim_error(e) from e
    out = []
    for place in data.get("places", []):
        sa = place.get("stop_area") or {}
        if not sa.get("id"):
            continue
        lines = []
        for ln in sa.get("lines", []) or []:
            lines.append({
                "id": ln.get("id"),
                "code": ln.get("code") or ln.get("name"),
                "mode": (ln.get("commercial_mode") or {}).get("name", ""),
                "color": ln.get("color") or "",
            })
        out.append({
            "id": sa["id"],
            "name": place.get("name") or sa.get("name"),
            "city": (sa.get("administrative_regions") or [{}])[0].get("name", ""),
            "lines": lines,
        })
    return {"stops": out, "age": age}


async def stop_lines(stop_id: str) -> dict:
    """Lineas que pasan por una parada, para elegir el tramo."""
    try:
        data, _ = await prim.get_client().lines_at_stop(stop_id)
    except PrimError as e:
        raise prim_error(e) from e
    out = [{
        "id": ln.get("id"),
        "code": ln.get("code") or ln.get("name"),
        "name": ln.get("name"),
        "mode": (ln.get("commercial_mode") or {}).get("name", ""),
        "color": ln.get("color") or "",
    } for ln in data.get("lines", [])]
    out.sort(key=mode_sort_key)
    return {"lines": out}


async def stop_directions(stop_id: str, line_id: str) -> dict:
    """Destinos que circulan AHORA para esa linea en esa parada."""
    try:
        data, age = await prim.get_client().stop_monitoring(sa_to_siri(stop_id))
    except PrimError as e:
        raise prim_error(e) from e
    return {"directions": B.observed_destinations(data, line_id), "age": age}


async def search_places(q: str) -> dict:
    try:
        data, _ = await prim.get_client().places(
            q, kinds=("stop_area", "address", "poi"), count=10)
    except PrimError as e:
        raise prim_error(e) from e
    out = []
    for place in data.get("places", []):
        kind = place.get("embedded_type") or ""
        body = place.get(kind) or {}
        pid = body.get("id") or place.get("id")
        if not pid:
            continue
        region = (body.get("administrative_regions") or [{}])[0]
        out.append({
            "id": pid,
            "name": place.get("name") or body.get("name") or "",
            "city": region.get("name", ""),
            "kind": {"stop_area": "parada", "address": "dirección",
                     "poi": "sitio"}.get(kind, kind),
        })
    return {"places": out}


# ---------------- tablero ----------------

async def board(route_id: int | None, log_history: bool) -> dict | None:
    """Tablero de una ruta (la que toca si no se indica).

    Devuelve None si no hay ninguna ruta guardada. El dict incluye las claves
    de la v1 y las internas (`_all_failed`, `_stations_failed`, `_error`):
    quien llama decide.
    """
    routes = await run_in_threadpool(db.list_routes)
    if not routes:
        return None

    if route_id is not None:
        route = await run_in_threadpool(db.get_route, route_id)
        if not route:
            raise ApiError("not_found", "ruta no encontrada")
    else:
        route = B.pick_active_route(routes)

    data = await B.build_board(route)

    # Todo lo que se ve en pantalla alimenta la prevision del anden, sin
    # gastar ni una llamada mas: son los mismos datos ya traidos. SQLite va
    # a un hilo aparte para no bloquear el bucle de eventos.
    try:
        await run_in_threadpool(_learn_platforms, data, route)
    except Exception as e:                # la prevision nunca tumba la pantalla
        log.warning("prevision de anden no disponible: %s", e)

    # Traduccion de los avisos: lo ya traducido sale al instante; lo nuevo se
    # encarga en segundo plano y aparece en el refresco siguiente. Nunca se
    # espera al modelo.
    try:
        await T.translate_board_async(data)
    except Exception as e:
        log.warning("traduccion no disponible: %s", e)

    # Sin ninguna estacion no hay pasos ni retrasos: una observacion asi
    # meteria un «0 min de retraso» falso en las estadisticas.
    if log_history and not data.get("_stations_failed"):
        try:
            await run_in_threadpool(B.record_history, route, data)
        except Exception as e:            # el historial nunca debe tumbar la pantalla
            log.warning("no se pudo guardar el historial: %s", e)

    data["auto_selected"] = route_id is None
    return data


def _learn_platforms(data: dict, route: dict) -> None:
    P.record_board(data, route)
    P.annotate(data, route)


# ---------------- alternativas ----------------

# Duracion de referencia de cada ruta, para poder decir "+12 min".
# Se calcula una vez y se guarda 12 h: no cambia de un dia para otro. Se
# olvida al editar o borrar la ruta.
_baseline: dict[int, tuple[float, int]] = {}
_BASELINE_TTL = 12 * 3600


async def alternatives(route_id: int, force: bool) -> dict:
    """Itinerarios alternativos, solo si hay una linea tocada (o se fuerza).

    Deliberadamente NO se llama en el refresco periodico: solo cuando hay
    perturbacion real o cuando se pide a mano.
    """
    route = await run_in_threadpool(db.get_route, route_id)
    if not route:
        raise ApiError("not_found", "ruta no encontrada")

    client = prim.get_client()
    try:
        gm, _ = await client.general_message()
        disruptions = B.index_disruptions(gm)
    except PrimError:
        disruptions = {}

    affected = []
    for leg in route["legs"]:
        st = B.line_status(line_code(leg["line_id"]), disruptions)
        if st["level"] > B.NORMAL:
            affected.append({
                "line_id": leg["line_id"],
                "line_code": leg["line_code"],
                "level": st["level"],
                "label": st["label"],
            })

    if not affected and not force:
        return {"needed": False, "affected": [], "options": []}

    forbidden = [a["line_id"] for a in affected]
    try:
        data, age = await client.journeys(route["origin_id"], route["dest_id"],
                                          forbidden=forbidden)
    except PrimError as e:
        raise prim_error(e, "el calculador") from e

    base = _baseline.get(route_id)
    baseline_seconds = base[0] if base and time.time() - base[1] < _BASELINE_TTL else None
    if baseline_seconds is None:
        try:
            plain, _ = await client.journeys(route["origin_id"], route["dest_id"])
            durations = [j["duration"] for j in plain.get("journeys", []) if j.get("duration")]
            if durations:
                baseline_seconds = float(min(durations))
                _baseline[route_id] = (baseline_seconds, int(time.time()))
        except PrimError:
            baseline_seconds = None

    options = []
    for j in data.get("journeys", [])[:6]:
        legs = []
        ok = True
        worst = B.NORMAL
        for s in j.get("sections", []):
            if s.get("type") != "public_transport":
                continue
            di = s.get("display_informations", {}) or {}
            lid = (s.get("links") or [{}])
            code = di.get("code") or di.get("label") or ""
            # El estado de la alternativa importa: no sirve mandarme por
            # otra linea que tambien esta caida.
            line_uri = next((l.get("id") for l in lid if l.get("type") == "line"), "")
            st = B.line_status(line_code(line_uri or ""), disruptions)
            worst = max(worst, st["level"])
            if st["level"] == B.INTERRUPTED:
                ok = False
            legs.append({
                "code": code,
                "mode": di.get("commercial_mode", ""),
                "direction": di.get("direction", ""),
                "color": di.get("color", ""),
                "minutes": round((s.get("duration") or 0) / 60),
                "status": st["label"],
            })
        if not legs:
            continue
        duration = j.get("duration") or 0
        delta = None
        if baseline_seconds and duration:
            delta = round((duration - baseline_seconds) / 60)
        options.append({
            "total_minutes": round(duration / 60),
            "transfers": j.get("nb_transfers", 0),
            "delta_minutes": delta,
            "legs": legs,
            "usable": ok,
            "worst_level": worst,
            "departure": j.get("departure_date_time", ""),
            "arrival": j.get("arrival_date_time", ""),
        })

    options.sort(key=lambda o: (not o["usable"], o["worst_level"], o["total_minutes"]))
    return {
        "needed": bool(affected),
        "affected": affected,
        "options": options[:2],
        "baseline_minutes": round(baseline_seconds / 60) if baseline_seconds else None,
        "age": age,
        "quota": dict(client.quota),
    }


# ---------------- planificador ----------------

async def plan(frm: str, to: str, when: str | None, mode: str) -> dict:
    """Opciones de trayecto entre dos sitios, para elegir una y guardarla."""
    if mode not in ("departure", "arrival"):
        raise ApiError("bad_request", "mode tiene que ser 'departure' o 'arrival'")
    if when and not _HHMM.match(when):
        raise ApiError("bad_request", "when tiene que ser HH:MM")
    try:
        data, age = await prim.get_client().journeys(
            frm, to, datetime_str=planner.when_param(when), count=4,
            represents=mode)
    except PrimError as e:
        raise prim_error(e) from e

    options = planner.parse_journeys(data, prefer_latest=(mode == "arrival"))
    if not options:
        raise ApiError("not_found",
                       "no hay ningún trayecto en transporte público entre esos dos "
                       "puntos a esa hora")
    return {"options": options, "age": round(age, 1)}


# Campos de texto de la opcion elegida (PlanLeg) y de `meta`. Se comprueban
# ANTES de montar la ruta: planner.route_from_option llama a PRIM con ellos y
# hace .strip()/int() sobre lo que llegue; un numero o un objeto donde va un
# texto era un 500 (H2 de la verificacion).
_PLAN_LEG_TEXT = ("line_id", "line_code", "line_name", "line_mode", "line_color",
                  "from_id", "from_name", "to_id", "to_name", "direction")
_META_TEXT = ("name", "origin_id", "origin_name", "dest_id", "dest_name",
              "time_mode", "time_at", "time_from", "time_to")


def _validate_plan(option: dict, meta: dict) -> None:
    for i, leg in enumerate(option["legs"]):
        for field in _PLAN_LEG_TEXT:
            v = leg.get(field)
            if v is not None and not isinstance(v, str):
                raise ApiError("bad_request", f"tramo {i + 1} del itinerario: '{field}' "
                                              f"tiene que ser texto")
    for field in _META_TEXT:
        v = meta.get(field)
        if v is not None and not isinstance(v, str):
            raise ApiError("bad_request", f"meta: '{field}' tiene que ser texto")
    minutes = option.get("minutes")
    if minutes is not None and not (_number(minutes) and 0 <= minutes <= 1440):
        raise ApiError("bad_request", "'minutes' del itinerario tiene que ser un numero de 0 a 1440")


async def route_from_plan(payload) -> dict:
    """Guarda como ruta vigilada la opcion que se ha elegido."""
    if not isinstance(payload, dict):
        raise ApiError("bad_request", "falta el itinerario elegido")
    option = payload.get("option") or {}
    meta = payload.get("meta") or {}
    if not isinstance(option, dict) or not isinstance(meta, dict) \
            or not isinstance(option.get("legs"), list) or not option.get("legs"):
        raise ApiError("bad_request", "falta el itinerario elegido")
    if not all(isinstance(l, dict) for l in option["legs"]):
        raise ApiError("bad_request", "tramos del itinerario no validos")
    _validate_plan(option, meta)
    data = await planner.route_from_option(option, meta)
    data = validate_route(data)
    route_id = await run_in_threadpool(db.save_route, data)
    mapdata.schedule_route(route_id)

    # Si algun tramo se queda sin direccion es que el texto de Navitia no
    # casaba con el del tiempo real: se avisa en vez de dejarlo en silencio.
    sin_dir = [l["line_code"] for l in data["legs"] if not l["directions"]]
    return {"id": route_id, "route": await run_in_threadpool(db.get_route, route_id),
            "without_direction": sin_dir}


# ---------------- estadisticas ----------------

async def platform_model(route_id: int | None) -> dict:
    """Que sabe la prevision del anden y si acierta."""
    out = {"accuracy": await run_in_threadpool(P.accuracy),
           "collector": collector.status()}
    routes = await run_in_threadpool(db.list_routes)
    rid = route_id if route_id is not None else (
        (B.pick_active_route(routes) or {}).get("id") if routes else None)
    if rid:
        route = await run_in_threadpool(db.get_route, rid)
        if route:
            out["route"] = {"id": route["id"], "name": route["name"]}
            out["coverage"] = await run_in_threadpool(P.coverage, route)
    return out


async def stats(days: int) -> dict:
    return await run_in_threadpool(db.stats, days)
