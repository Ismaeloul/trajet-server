"""Planificador puerta a puerta: de una direccion a otra, como en un mapa.

La idea es la de siempre en esta app: la API manda. Navitia devuelve varios
itinerarios y aqui solo se traducen a algo que quepa en una pantalla de movil
y que luego se pueda guardar como ruta vigilada.

La parte delicada es la direccion del tramo. Navitia dice "La Defense
(Puteaux)" y el tiempo real (SIRI) dice "La Defense" a secas, asi que copiar
el texto tal cual dejaria el tablero vacio. Por eso, al guardar, la direccion
se contrasta con los destinos que circulan de verdad ahora mismo.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta

from . import prim
from .board import observed_destinations
from .config import settings
from .idfm import norm_text, sa_to_siri

# Secciones que son "ir andando". El resto que no sea public_transport son
# esperas o enlaces sin interes para lo que vigilamos.
WALK_TYPES = {"street_network", "crow_fly", "transfer"}


def _link(section: dict, kind: str) -> str:
    for l in section.get("links", []) or []:
        if l.get("type") == kind:
            return l.get("id", "")
    return ""


def _stop_area(end: dict) -> tuple[str, str]:
    """Del extremo de una seccion saca (id de zona de parada, nombre)."""
    sp = end.get("stop_point") or {}
    sa = sp.get("stop_area") or end.get("stop_area") or {}
    if sa.get("id"):
        return sa["id"], sa.get("name") or sp.get("name") or ""
    return end.get("id", ""), end.get("name", "")


# ---------------- coordenadas de las paradas ----------------

# El mapa de la ruta usa las coordenadas de subida y bajada de cada tramo como
# ultimo recurso (si el portal de datos abiertos no tiene la parada). Navitia
# las trae en cada seccion del itinerario, pero la opcion que se devuelve a la
# app (PlanLeg en docs/openapi.yaml) es cerrada: no admite campos nuevos. Asi
# que se apuntan aqui al leer los itinerarios y route_from_option las recupera
# al guardar la opcion elegida. Solo en memoria y acotado: si el servidor se
# reinicia entre planificar y guardar, la ruta se guarda sin ellas y el mapa
# tira de los datos abiertos, como con cualquier ruta hecha a mano.
_COORDS_MAX = 2000
_coords: OrderedDict[tuple[str, str], tuple[float, float]] = OrderedDict()


def _coord_of(end: dict) -> tuple[float, float] | None:
    """(lat, lon) de un extremo de seccion de Navitia.

    Primero la del punto de parada exacto (el anden de ESA linea: en
    Saint-Lazare el tren y el bus paran en sitios distintos), luego la de la
    zona de parada y por ultimo la del propio extremo (una direccion).
    """
    def obj(value) -> dict:
        return value if isinstance(value, dict) else {}

    end = obj(end)
    sp = obj(end.get("stop_point"))
    kind = end.get("embedded_type")
    body = obj(end.get(kind)) if isinstance(kind, str) else {}
    for c in (body.get("coord"), sp.get("coord"), obj(sp.get("stop_area")).get("coord"),
              obj(end.get("stop_area")).get("coord"), end.get("coord")):
        if not isinstance(c, dict):
            continue
        try:
            lat, lon = float(c["lat"]), float(c["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180 and (lat or lon):
            return lat, lon
    return None


def _remember_coord(line_id: str, stop_id: str, coord: tuple[float, float] | None) -> None:
    if not (stop_id and coord):
        return
    # Por (linea, parada) y, de reserva, por parada sola.
    for key in ((line_id, stop_id), ("", stop_id)):
        _coords[key] = coord
        _coords.move_to_end(key)
    while len(_coords) > _COORDS_MAX:
        _coords.popitem(last=False)


def leg_coords(line_id: str, from_id: str, to_id: str) -> dict:
    """from_lat/from_lon/to_lat/to_lon conocidos de un tramo (solo los que haya)."""
    out: dict[str, float] = {}
    for prefix, stop in (("from", from_id), ("to", to_id)):
        c = _coords.get((line_id, stop)) or _coords.get(("", stop))
        if c:
            out[f"{prefix}_lat"], out[f"{prefix}_lon"] = c
    return out


def _hhmm(value: str | None) -> str:
    """Navitia manda '20260831T083100'."""
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").strftime("%H:%M")
    except ValueError:
        return ""


def parse_journeys(payload: dict, prefer_latest: bool = False) -> list[dict]:
    """Traduce la respuesta de /journeys a opciones para elegir.

    prefer_latest cambia el orden cuando se planifica por hora de llegada:
    ahi lo util no es el mas rapido sino el que te deja salir mas tarde y aun
    asi llegar a tiempo.
    """
    out: list[dict] = []
    for j in payload.get("journeys", []) or []:
        legs: list[dict] = []
        walk = 0
        for s in j.get("sections", []) or []:
            stype = s.get("type")
            if stype in WALK_TYPES:
                walk += int(s.get("duration") or 0)
                continue
            if stype != "public_transport":
                continue
            di = s.get("display_informations") or {}
            frm_id, frm_name = _stop_area(s.get("from") or {})
            to_id, to_name = _stop_area(s.get("to") or {})
            line_id = _link(s, "line")
            _remember_coord(line_id, frm_id, _coord_of(s.get("from") or {}))
            _remember_coord(line_id, to_id, _coord_of(s.get("to") or {}))
            legs.append({
                "line_id": line_id,
                "line_code": di.get("code") or di.get("label") or "",
                "line_name": di.get("name") or di.get("label") or "",
                "line_mode": di.get("commercial_mode") or di.get("physical_mode") or "",
                "line_color": di.get("color") or "",
                "from_id": frm_id, "from_name": frm_name,
                "to_id": to_id, "to_name": to_name,
                # Direccion segun Navitia. Se traduce a la de SIRI al guardar.
                "direction": di.get("direction") or di.get("headsign") or "",
                "minutes": round(int(s.get("duration") or 0) / 60),
                "at": _hhmm(s.get("departure_date_time")),
            })

        # Un itinerario andando entero no es algo que se pueda vigilar.
        if not legs:
            continue

        out.append({
            "kind": j.get("type") or "",
            "minutes": round(int(j.get("duration") or 0) / 60),
            "walk_minutes": round(walk / 60),
            "transfers": int(j.get("nb_transfers") or 0),
            "departure": _hhmm(j.get("departure_date_time")),
            "arrival": _hhmm(j.get("arrival_date_time")),
            "legs": legs,
        })

    if prefer_latest:
        # Salir lo mas tarde posible; a igualdad, menos transbordos.
        out.sort(key=lambda o: (o["departure"], -o["transfers"]), reverse=True)
    else:
        # El mas rapido primero; a igualdad, el que tenga menos transbordos.
        out.sort(key=lambda o: (o["minutes"], o["transfers"]))
    # Navitia repite itinerarios casi iguales; nos quedamos con uno por
    # combinacion de lineas, que es lo que de verdad distingue una opcion.
    seen: set[tuple] = set()
    unique = []
    for o in out:
        key = tuple(l["line_id"] for l in o["legs"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(o)
    return unique


async def resolve_direction(from_id: str, line_id: str, wanted: str) -> list[str]:
    """Traduce la direccion de Navitia al texto que usa el tiempo real.

    Devuelve lista vacia si no encuentra equivalencia: mas vale ensenar todos
    los pasos de la linea que un tablero vacio sin explicacion.
    """
    if not (from_id and line_id and wanted):
        return []
    try:
        payload, _ = await prim.get_client().stop_monitoring(sa_to_siri(from_id))
    except Exception:
        return []

    live = observed_destinations(payload, line_id)
    if not live:
        return []

    target = norm_text(wanted)
    # 1) igual; 2) uno contiene al otro ("La Defense" vs "La Defense (Puteaux)")
    for dest in live:
        if norm_text(dest) == target:
            return [dest]
    for dest in live:
        n = norm_text(dest)
        if n and (n in target or target in n):
            return [dest]
    # 3) por la primera palabra fuerte, que es donde suelen coincidir
    head = target.split(" (")[0].strip()
    for dest in live:
        if head and norm_text(dest).startswith(head[: max(6, len(head) // 2)]):
            return [dest]
    return []


async def route_from_option(option: dict, meta: dict) -> dict:
    """Monta el dict que espera db.save_route a partir de una opcion elegida."""
    legs = option.get("legs") or []
    dirs = await asyncio.gather(*[
        resolve_direction(l.get("from_id", ""), l.get("line_id", ""),
                          l.get("direction", ""))
        for l in legs
    ])

    out_legs = []
    for leg, resolved in zip(legs, dirs):
        out = {
            "line_id": leg.get("line_id", ""),
            "line_code": leg.get("line_code", ""),
            "line_name": leg.get("line_name", ""),
            "line_mode": leg.get("line_mode", ""),
            "line_color": leg.get("line_color", ""),
            "from_id": leg.get("from_id", ""),
            "from_name": leg.get("from_name", ""),
            "to_id": leg.get("to_id", ""),
            "to_name": leg.get("to_name", ""),
            "directions": resolved,
        }
        # Coordenadas de subida y bajada para el mapa (db.save_route las
        # guarda si vienen): las del itinerario de Navitia que se acaba de
        # ensenar; si el tramo ya las trae, mandan las suyas.
        out.update(leg_coords(out["line_id"], out["from_id"], out["to_id"]))
        for k in ("from_lat", "from_lon", "to_lat", "to_lon"):
            if isinstance(leg.get(k), (int, float)) and not isinstance(leg.get(k), bool):
                out[k] = float(leg[k])
        out_legs.append(out)

    first = legs[0]
    last = legs[-1]
    return {
        "name": (meta.get("name") or "").strip()
                or f"{meta.get('origin_name') or first['from_name']} → "
                   f"{meta.get('dest_name') or last['to_name']}",
        "origin_id": meta.get("origin_id") or first.get("from_id", ""),
        "origin_name": meta.get("origin_name") or first.get("from_name", ""),
        "dest_id": meta.get("dest_id") or last.get("to_id", ""),
        "dest_name": meta.get("dest_name") or last.get("to_name", ""),
        "days": meta.get("days") or [0, 1, 2, 3, 4],
        "time_from": meta.get("time_from") or "07:00",
        "time_to": meta.get("time_to") or "10:00",
        "time_mode": meta.get("time_mode") or "window",
        "time_at": meta.get("time_at") or "",
        # La duracion real del itinerario elegido: con ella la franja que se
        # calcula a partir de "quiero llegar a las 09:00" es la de verdad y
        # no una estimacion.
        "duration_min": int(option.get("minutes") or 0),
        "legs": out_legs,
    }


def when_param(when: str | None, now: datetime | None = None) -> str | None:
    """Convierte 'HH:MM' o vacio al formato de Navitia, en hora de Paris.

    Si esa hora ya ha pasado hoy, se planifica para manana: buscar a las
    23:00 un trayecto "para las 08:00" quiere decir manana por la manana, y
    con la fecha de hoy Navitia devolvia los trenes de esta manana, ya idos.
    """
    if not when:
        return None
    now = now or datetime.now(settings.tz)
    try:
        hh, mm = (int(x) for x in when.split(":", 1))
        # Dentro del try: "25:00" tambien es una hora que no existe (en la
        # 0.3.0 reventaba aqui con un 500; la API ya lo para antes con 400).
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return None
    if target < now:
        target += timedelta(days=1)
    return target.strftime("%Y%m%dT%H%M%S")
