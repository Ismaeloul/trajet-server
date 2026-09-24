"""Rutas de la 0.3.0 (/api/*), con el mismo comportamiento de siempre.

Se conservan por compatibilidad (el encargo: «todas las rutas actuales siguen
funcionando»). En Umbrel quedan detras del login, igual que el panel: la app
del iPhone usa /api/v1. Los nombres de las funciones son los de la 0.3.0 para
que el openapi.json generado siga siendo el mismo.

Diferencias con la 0.3.0 (arreglos, no cambios de contrato):
  - Entradas malas dan 400 con motivo en vez de 500.
  - `when` fuera de rango en /api/plan da 400.
  - Si fallan los avisos, `errors` lo dice (antes: todo «normal» en silencio).
  - Solo se aceptan conexiones del proxy de Umbrel (o 127.0.0.1), como en el
    panel: otra app de la red Docker se saltaria el login (403).

Lo que NO cambia: cualquier fallo de PRIM sigue siendo 502 con el texto de
siempre («la API de IDFM no responde: …», «el calculador no responde: …» en
las alternativas). Los 503 y los codigos nuevos (sin clave, clave rechazada,
cuota agotada) son solo de la v1: un cliente de la 0.3.0 no los conoce.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

from .. import auth, prim
from .. import board as B
from .. import platform as P
from .. import translate as T
from ..collector import collector
from . import common
from .errors import ApiError

router = APIRouter(dependencies=[Depends(auth.require_proxy_peer)])


def _raise(e: ApiError):
    # Un fallo de PRIM lleva su forma de la 0.3.0 (common.prim_error): 502 y
    # «<quien> no responde: <motivo>», sea cual sea el codigo de la v1.
    if e.legacy is not None:
        status, message = e.legacy
        raise HTTPException(status, message) from e
    raise HTTPException(e.status, e.message, headers=e.headers or None) from e


# ---------------- salud ----------------

@router.get("/api/health")
async def health():
    client = prim.get_client()
    return {
        "ok": True,
        "key_configured": bool(client.has_key()),
        "quota": dict(client.quota),
        "last_error": client.last_error,
        "now_paris": B.now_paris().strftime("%Y-%m-%d %H:%M:%S"),
        "collector": collector.status(),
        "platform_model": await run_in_threadpool(P.accuracy),
        "translator": await T.available_cached(),
    }


# ---------------- rutas guardadas ----------------

@router.get("/api/routes")
async def api_routes():
    return await run_in_threadpool(common.list_routes)


@router.post("/api/routes")
async def api_create_route(payload: dict):
    try:
        return await run_in_threadpool(common.create_route, payload)
    except ApiError as e:
        _raise(e)


@router.put("/api/routes/{route_id}")
async def api_update_route(route_id: int, payload: dict):
    try:
        return await run_in_threadpool(common.update_route, route_id, payload)
    except ApiError as e:
        _raise(e)


@router.delete("/api/routes/{route_id}")
async def api_delete_route(route_id: int):
    try:
        return await run_in_threadpool(common.delete_route, route_id)
    except ApiError as e:
        _raise(e)


# ---------------- buscador para dar de alta rutas ----------------

@router.get("/api/search/stops")
async def api_search_stops(q: str = Query(min_length=2)):
    try:
        return await common.search_stops(q)
    except ApiError as e:
        _raise(e)


@router.get("/api/stops/{stop_id}/lines")
async def api_stop_lines(stop_id: str):
    """Lineas que pasan por una parada, para elegir el tramo."""
    try:
        return await common.stop_lines(stop_id)
    except ApiError as e:
        _raise(e)


@router.get("/api/stops/{stop_id}/directions")
async def api_stop_directions(stop_id: str, line_id: str):
    """Destinos que circulan AHORA para esa linea en esa parada."""
    try:
        return await common.stop_directions(stop_id, line_id)
    except ApiError as e:
        _raise(e)


# ---------------- tablero ----------------

@router.get("/api/board")
async def api_board(route_id: int | None = None, log_history: bool = True):
    try:
        data = await common.board(route_id, log_history)
    except ApiError as e:
        _raise(e)
    if data is None:
        return {"empty": True, "message": "todavia no hay rutas guardadas"}
    return common.to_legacy_board(data)


@router.get("/api/alternatives/{route_id}")
async def api_alternatives(route_id: int, force: bool = False):
    """Itinerarios alternativos, solo si hay una linea tocada."""
    try:
        return await common.alternatives(route_id, force)
    except ApiError as e:
        _raise(e)


# ---------------- planificador ----------------

@router.get("/api/search/places")
async def api_search_places(q: str = Query(min_length=2)):
    """Busca paradas Y direcciones postales, para el planificador."""
    try:
        return await common.search_places(q)
    except ApiError as e:
        _raise(e)


@router.get("/api/plan")
async def api_plan(frm: str = Query(alias="from", min_length=3),
                   to: str = Query(min_length=3),
                   when: str | None = None,
                   mode: str = "departure"):
    """Opciones de trayecto entre dos sitios, para elegir una y guardarla."""
    try:
        return await common.plan(frm, to, when, mode)
    except ApiError as e:
        _raise(e)


@router.post("/api/routes/from-plan")
async def api_route_from_plan(payload: dict):
    """Guarda como ruta vigilada la opcion que se ha elegido."""
    try:
        return await common.route_from_plan(payload)
    except ApiError as e:
        _raise(e)


# ---------------- estadisticas ----------------

@router.get("/api/platform-model")
async def api_platform_model(route_id: int | None = None):
    """Que sabe la prevision del anden y si acierta."""
    return await common.platform_model(route_id)


@router.get("/api/stats")
async def api_stats(days: int = 90):
    return await common.stats(days)
