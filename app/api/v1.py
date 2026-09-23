"""API v1 para la app del iPhone (/api/v1/*), segun docs/openapi.yaml.

Fuera del login de Umbrel (PROXY_AUTH_WHITELIST "/api/v1/*"), asi que aqui
manda el token de dispositivo: todo lo que cuelga de `_private` pasa por
auth.require_device. Solo /ping y /pair van sin token.

La logica vive en api/common.py (la misma que usa la 0.3.0) y en auth.py;
aqui solo hay HTTP: leer y validar parametros, llamar y dar la forma del
contrato. Los errores son ApiError y errors.py los convierte en ErrorV1. El
ETag/304 lo pone el middleware (api/etag.py) y el gzip, GZipMiddleware.

Los cuerpos JSON se leen a mano, DESPUES de comprobar el token: un cuerpo
malo de alguien sin token recibe 401, no un 400 que le cuente algo.
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth, db, mapdata, prim
from .. import board as B
from .. import platform as P
from .. import translate as T
from ..collector import collector
from ..config import API_VERSION, VERSION
from ..prim import PrimError
from . import common
from .errors import ApiError

router = APIRouter(prefix="/api/v1")
_private = APIRouter(dependencies=[Depends(auth.require_device)])

# Topes de tamano del cuerpo: /pair es publico (cualquiera en la red puede
# mandar lo que quiera) y una ruta con muchos tramos no llega ni a 20 KB.
_MAX_PAIR_BODY = 4 * 1024
_MAX_BODY = 256 * 1024

_EMPTY_MESSAGE = "todavía no hay rutas guardadas"


async def _json_body(request: Request, limit: int = _MAX_BODY):
    """El cuerpo como JSON, o ApiError bad_request (nunca 422 ni 500)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ApiError("bad_request", "el cuerpo es demasiado grande")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise ApiError("bad_request", "el cuerpo es demasiado grande")
    if not raw:
        raise ApiError("bad_request", "falta el cuerpo JSON")
    try:
        return json.loads(bytes(raw))
    except (ValueError, UnicodeDecodeError) as e:
        raise ApiError("bad_request", "el cuerpo no es JSON válido") from e


def _no_store(body: dict, status: int = 200) -> JSONResponse:
    # Lleva un token o datos que no deben quedarse en ninguna cache.
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _clean_q(q: str) -> str:
    q = q.strip()
    if len(q) < 2:
        raise ApiError("bad_request", "escribe al menos 2 caracteres")
    return q


# =====================================================================
#  Sin token
# =====================================================================

@router.get("/ping", operation_id="v1_ping")
async def v1_ping(request: Request):
    """¿Hay un Trajet aqui? Nunca 401 y nunca gasta cuota.

    Sin token no toca la BD; con token hace una sola lectura para decir si
    vale (sin apuntar el ultimo uso)."""
    device = await auth.optional_device(request)
    return {"ok": True, "service": "trajet", "api": API_VERSION, "version": VERSION,
            "paired": device is not None}


def _pair_text(body: dict, field: str, required: bool, min_len: int, max_len: int) -> str:
    value = body.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ApiError("bad_request", f"falta '{field}' o no es texto")
    n = len(value.strip()) if field != "code" else len(value)
    if n < min_len or n > max_len:
        raise ApiError("bad_request", f"'{field}' tiene que tener de {min_len} a {max_len} caracteres")
    return value


@router.post("/pair", operation_id="v1_pair")
async def v1_pair(request: Request):
    """Canjea el codigo del QR por un token de dispositivo.

    Mal escrito, caducado, usado o anulado: la MISMA respuesta
    (pairing_invalid). Rate limit en auth.redeem (429 + Retry-After)."""
    body = await _json_body(request, _MAX_PAIR_BODY)
    if not isinstance(body, dict):
        raise ApiError("bad_request", "el cuerpo tiene que ser un objeto JSON")
    # Solo se valida la forma (PairRequest); los campos que sobren se ignoran
    # para que una version nueva de la app no se quede sin poder emparejar.
    code = _pair_text(body, "code", True, 8, 16)
    name = _pair_text(body, "device_name", True, 1, auth.MAX_DEVICE_NAME)
    model = _pair_text(body, "device_model", False, 0, auth.MAX_DEVICE_MODEL)
    appv = _pair_text(body, "app_version", False, 0, auth.MAX_APP_VERSION)

    def _redeem():
        return auth.redeem(code, name, model, appv, auth.client_ip(request))

    return _no_store(await run_in_threadpool(_redeem))


# =====================================================================
#  Con token
# =====================================================================

# ---------------- dispositivo ----------------

@_private.get("/devices/me", operation_id="v1_device_me")
async def v1_device_me(device: dict = Depends(auth.require_device)):
    return _no_store(auth.public_device(device))


@_private.delete("/devices/me", operation_id="v1_device_unpair")
async def v1_device_unpair(device: dict = Depends(auth.require_device)):
    """Desempareja este iPhone: su token deja de valer en la siguiente peticion."""
    await run_in_threadpool(auth.revoke_device, device["id"])
    return {"revoked": True}


# ---------------- salud ----------------

def _server_snapshot() -> tuple[dict, dict]:
    # prim_state y quota_snapshot pueden leer SQLite (cuota persistida) o el
    # almacen de la clave: van juntos a un hilo.
    return prim.prim_state(), prim.quota_snapshot()


def _schema_version() -> int | None:
    try:
        return db.schema_version()
    except Exception:
        return None


@_private.get("/health", operation_id="v1_health")
async def v1_health():
    """Estado del servidor, de la clave PRIM y de la cuota. No gasta cuota y
    nunca devuelve la clave ni un trozo de ella."""
    from ..migrations import LATEST

    schema = await run_in_threadpool(_schema_version)
    prim_st, quota = await run_in_threadpool(_server_snapshot)
    return {
        # ok = el esquema de la BD esta al dia (si una migracion fallo, el
        # servidor arranca en modo degradado y aqui se ve).
        "ok": schema is not None and schema >= LATEST,
        "version": VERSION,
        "api": API_VERSION,
        "now_paris": B.now_paris().strftime("%Y-%m-%d %H:%M:%S"),
        "schema_version": schema or 0,
        "prim": prim_st,
        "quota": quota,
        "collector": collector.status(),
        "platform_model": await run_in_threadpool(P.accuracy),
        "translator": await T.available_cached(),
    }


# ---------------- tablero ----------------

@_private.get("/board", operation_id="v1_board")
async def v1_board(route_id: int | None = None, log_history: bool = True):
    """Tablero de una ruta (la que toca si no se indica).

    Nunca un tablero hueco (R9, R88): si no se pudo leer ninguna estacion ni
    en cache, error con codigo (prim_key_missing, prim_unreachable…) para
    que la app se quede con su ultimo tablero bueno y enseñe el estado
    diseñado. Si falla solo una parte, 200 con esa parte en `errors`."""
    data = await common.board(route_id, log_history)
    server = prim.server_state()             # solo memoria (prim.py)
    if data is None:
        return {"empty": True, "message": _EMPTY_MESSAGE, "server": server}
    if data.get("_all_failed"):
        err = data.get("_error")
        if not isinstance(err, Exception):
            err = PrimError("no se pudo leer ninguna estación", "unreachable")
        raise common.prim_error(err)
    common.strip_private(data)
    data["server"] = server
    return data


@_private.get("/alternatives/{route_id}", operation_id="v1_alternatives")
async def v1_alternatives(route_id: int, force: bool = False):
    """Solo al pedirlas (R29): nunca en el refresco del tablero."""
    return await common.alternatives(route_id, force)


# ---------------- rutas ----------------

@_private.get("/routes", operation_id="v1_routes_list")
async def v1_routes_list():
    return await run_in_threadpool(common.list_routes)


@_private.post("/routes", operation_id="v1_routes_create", status_code=201)
async def v1_routes_create(request: Request):
    payload = await _json_body(request)
    return JSONResponse(await run_in_threadpool(common.create_route, payload), status_code=201)


# Antes que /routes/{route_id}: el orden de registro decide el emparejado.
@_private.post("/routes/from-plan", operation_id="v1_routes_from_plan", status_code=201)
async def v1_routes_from_plan(request: Request):
    payload = await _json_body(request)
    return JSONResponse(await common.route_from_plan(payload), status_code=201)


@_private.get("/routes/{route_id}", operation_id="v1_routes_get")
async def v1_routes_get(route_id: int):
    return await run_in_threadpool(common.get_route, route_id)


@_private.put("/routes/{route_id}", operation_id="v1_routes_update")
async def v1_routes_update(route_id: int, request: Request):
    payload = await _json_body(request)
    return await run_in_threadpool(common.update_route, route_id, payload)


@_private.delete("/routes/{route_id}", operation_id="v1_routes_delete")
async def v1_routes_delete(route_id: int):
    return await run_in_threadpool(common.delete_route, route_id)


@_private.get("/routes/{route_id}/map", operation_id="v1_route_map")
async def v1_route_map(route_id: int):
    """Mapa de la ruta con los datos abiertos de IDFM (no gasta cuota de PRIM)."""
    route = await run_in_threadpool(db.get_route, route_id, True)
    if not route:
        raise ApiError("not_found", "ruta no encontrada")
    return await mapdata.route_map(route)


# ---------------- buscador ----------------

@_private.get("/search/stops", operation_id="v1_search_stops")
async def v1_search_stops(q: str = Query(min_length=2, max_length=80)):
    return await common.search_stops(_clean_q(q))


@_private.get("/search/places", operation_id="v1_search_places")
async def v1_search_places(q: str = Query(min_length=2, max_length=80)):
    return await common.search_places(_clean_q(q))


@_private.get("/stops/{stop_id}/lines", operation_id="v1_stop_lines")
async def v1_stop_lines(stop_id: str):
    return await common.stop_lines(stop_id)


@_private.get("/stops/{stop_id}/directions", operation_id="v1_stop_directions")
async def v1_stop_directions(stop_id: str, line_id: str = Query(min_length=1)):
    return await common.stop_directions(stop_id, line_id)


# ---------------- planificador ----------------

@_private.get("/plan", operation_id="v1_plan")
async def v1_plan(frm: str = Query(alias="from", min_length=3),
                  to: str = Query(min_length=3),
                  when: str | None = Query(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$"),
                  mode: Literal["departure", "arrival"] = "departure"):
    """`when` fuera de rango (25:00) da 400, no 500 como en la 0.3.0."""
    return await common.plan(frm, to, when, mode)


# ---------------- estadisticas ----------------

@_private.get("/stats", operation_id="v1_stats")
async def v1_stats(days: int = Query(90, ge=1, le=3650)):
    return await common.stats(days)


@_private.get("/platform-model", operation_id="v1_platform_model")
async def v1_platform_model(route_id: int | None = None):
    """Unica fuente del porcentaje de acierto de la vía (R11)."""
    return await common.platform_model(route_id)


# Al final: include_router copia las rutas que haya en ese momento.
router.include_router(_private)
