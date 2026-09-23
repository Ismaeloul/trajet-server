"""Trajet - API y servidor de la interfaz."""
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import board as B
from . import db, planner, prim, translate as T
from . import platform as P
from .collector import collector
from .config import settings
from .idfm import line_code, mode_sort_key, sa_to_siri
from .prim import PrimError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trajet")

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.api_key:
        log.error("PRIM_API_KEY vacia: la app arranca pero no podra "
                  "consultar nada. Revisa el .env.")
    db.init()
    prim.client = prim.PrimClient(settings.api_key)
    await prim.client.start()
    collector.start()
    log.info("Trajet listo. BD en %s", settings.db_path)
    yield
    await collector.stop()
    await prim.client.close()


app = FastAPI(title="Trajet", lifespan=lifespan, docs_url=None, redoc_url=None)


# ---------------- salud ----------------

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "key_configured": bool(settings.api_key),
        "quota": dict(prim.get_client().quota),
        "last_error": prim.get_client().last_error,
        "now_paris": B.now_paris().strftime("%Y-%m-%d %H:%M:%S"),
        "collector": collector.status(),
        "platform_model": await run_in_threadpool(P.accuracy),
        "translator": await T.available(),
    }


# ---------------- rutas guardadas ----------------

@app.get("/api/routes")
async def api_routes():
    routes = db.list_routes()
    active = B.pick_active_route(routes)
    return {"routes": routes, "active_id": active["id"] if active else None}


@app.post("/api/routes")
async def api_create_route(payload: dict):
    _validate_route(payload)
    rid = db.save_route(payload)
    return {"id": rid, "route": db.get_route(rid)}


@app.put("/api/routes/{route_id}")
async def api_update_route(route_id: int, payload: dict):
    if not db.get_route(route_id):
        raise HTTPException(404, "ruta no encontrada")
    _validate_route(payload)
    db.save_route(payload, route_id)
    return {"id": route_id, "route": db.get_route(route_id)}


@app.delete("/api/routes/{route_id}")
async def api_delete_route(route_id: int):
    if not db.delete_route(route_id):
        raise HTTPException(404, "ruta no encontrada")
    return {"deleted": route_id}


def _validate_route(p: dict):
    for field in ("name", "origin_id", "dest_id"):
        if not str(p.get(field, "")).strip():
            raise HTTPException(400, f"falta el campo '{field}'")
    if not p.get("legs"):
        raise HTTPException(400, "la ruta necesita al menos un tramo")
    for i, leg in enumerate(p["legs"]):
        for field in ("line_id", "from_id"):
            if not str(leg.get(field, "")).strip():
                raise HTTPException(400, f"tramo {i + 1}: falta '{field}'")


# ---------------- buscador para dar de alta rutas ----------------

@app.get("/api/search/stops")
async def api_search_stops(q: str = Query(min_length=2)):
    try:
        data, age = await prim.get_client().places(q)
    except PrimError as e:
        raise HTTPException(502, f"la API de IDFM no responde: {e}")
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


@app.get("/api/stops/{stop_id}/lines")
async def api_stop_lines(stop_id: str):
    """Lineas que pasan por una parada, para elegir el tramo."""
    try:
        data, _ = await prim.get_client().lines_at_stop(stop_id)
    except PrimError as e:
        raise HTTPException(502, f"la API de IDFM no responde: {e}")
    out = [{
        "id": ln.get("id"),
        "code": ln.get("code") or ln.get("name"),
        "name": ln.get("name"),
        "mode": (ln.get("commercial_mode") or {}).get("name", ""),
        "color": ln.get("color") or "",
    } for ln in data.get("lines", [])]
    out.sort(key=mode_sort_key)
    return {"lines": out}


@app.get("/api/stops/{stop_id}/directions")
async def api_stop_directions(stop_id: str, line_id: str):
    """Destinos que circulan AHORA para esa linea en esa parada.

    Asi no hay que escribir la direccion a mano: se elige de lo que hay.
    """
    try:
        data, age = await prim.get_client().stop_monitoring(sa_to_siri(stop_id))
    except PrimError as e:
        raise HTTPException(502, f"la API de IDFM no responde: {e}")
    return {"directions": B.observed_destinations(data, line_id), "age": age}


# ---------------- tablero ----------------

@app.get("/api/board")
async def api_board(route_id: int | None = None, log_history: bool = True):
    routes = db.list_routes()
    if not routes:
        return {"empty": True, "message": "todavia no hay rutas guardadas"}

    route = None
    if route_id is not None:
        route = db.get_route(route_id)
        if not route:
            raise HTTPException(404, "ruta no encontrada")
    else:
        route = B.pick_active_route(routes)

    try:
        data = await B.build_board(route)
    except PrimError as e:
        raise HTTPException(502, f"la API de IDFM no responde: {e}")

    # Todo lo que se ve en pantalla alimenta la prevision del anden, sin
    # gastar ni una llamada mas: son los mismos datos ya traidos.
    #
    # Es SQLite sincrono (hasta tres consultas por salida) y va a un hilo
    # aparte: en el bucle de eventos bloquearia cualquier otra peticion
    # mientras el disco del NAS responde.
    try:
        await run_in_threadpool(_learn_platforms, data, route)
    except Exception as e:                # la prevision nunca tumba la pantalla
        log.warning("prevision de anden no disponible: %s", e)

    # Traduccion de los avisos. Lo ya traducido sale al instante; lo nuevo se
    # encarga en segundo plano y aparece en el refresco siguiente. Nunca se
    # espera al modelo: la pantalla es lo primero.
    try:
        T.translate_board(data)
    except Exception as e:
        log.warning("traduccion no disponible: %s", e)

    if log_history:
        try:
            await run_in_threadpool(B.record_history, route, data)
        except Exception as e:            # el historial nunca debe tumbar la pantalla
            log.warning("no se pudo guardar el historial: %s", e)

    data["auto_selected"] = route_id is None
    return data


def _learn_platforms(data: dict, route: dict) -> None:
    P.record_board(data, route)
    P.annotate(data, route)


# ---------------- PASO 2: alternativas ----------------

# Duracion de referencia de cada ruta, para poder decir "+12 min".
# Se calcula una vez y se guarda 12 h: no cambia de un dia para otro.
_baseline: dict[int, tuple[float, int]] = {}
_BASELINE_TTL = 12 * 3600


@app.get("/api/alternatives/{route_id}")
async def api_alternatives(route_id: int, force: bool = False):
    """Itinerarios alternativos, solo si hay una linea tocada.

    Deliberadamente NO se llama en el refresco periodico: solo cuando hay
    perturbacion real o cuando se pide a mano. El calculador comparte la
    bolsa de 1000 llamadas/dia con el buscador de paradas.
    """
    route = db.get_route(route_id)
    if not route:
        raise HTTPException(404, "ruta no encontrada")

    client = prim.get_client()
    try:
        gm, _ = await client.general_message()
        disruptions = B.index_disruptions(gm)
    except PrimError:
        disruptions = {}

    # Lineas de la ruta que estan tocadas
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
        raise HTTPException(502, f"el calculador no responde: {e}")

    # Referencia: cuanto dura normalmente, sin excluir nada
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
            di = s.get("display_informations", {})
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
                "minutes": round(s.get("duration", 0) / 60),
                "status": st["label"],
            })
        if not legs:
            continue
        total = round(j.get("duration", 0) / 60)
        delta = None
        if baseline_seconds:
            delta = round((j["duration"] - baseline_seconds) / 60)
        options.append({
            "total_minutes": total,
            "transfers": j.get("nb_transfers", 0),
            "delta_minutes": delta,
            "legs": legs,
            "usable": ok,
            "worst_level": worst,
            "departure": j.get("departure_date_time", ""),
            "arrival": j.get("arrival_date_time", ""),
        })

    # Primero las que se pueden usar, luego las mas rapidas
    options.sort(key=lambda o: (not o["usable"], o["worst_level"], o["total_minutes"]))
    return {
        "needed": bool(affected),
        "affected": affected,
        "options": options[:2],
        "baseline_minutes": round(baseline_seconds / 60) if baseline_seconds else None,
        "age": age,
        "quota": dict(client.quota),
    }


# ---------------- PASO 3: estadisticas ----------------

@app.get("/api/search/places")
async def api_search_places(q: str = Query(min_length=2)):
    """Busca paradas Y direcciones postales, para el planificador.

    Navitia identifica una direccion por sus coordenadas ("lon;lat") y ese
    mismo identificador sirve luego como origen o destino del itinerario.
    """
    try:
        data, _ = await prim.get_client().places(
            q, kinds=("stop_area", "address", "poi"), count=10)
    except PrimError as e:
        raise HTTPException(502, f"la API de IDFM no responde: {e}")

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


@app.get("/api/plan")
async def api_plan(frm: str = Query(alias="from", min_length=3),
                   to: str = Query(min_length=3),
                   when: str | None = None,
                   mode: str = "departure"):
    """Opciones de trayecto entre dos sitios, para elegir una y guardarla.

    mode='arrival' planifica hacia atras: "quiero estar alli a las 09:00".
    Es como se piensa de verdad el trayecto al trabajo.
    """
    if mode not in ("departure", "arrival"):
        raise HTTPException(400, "mode tiene que ser 'departure' o 'arrival'")
    try:
        data, age = await prim.get_client().journeys(
            frm, to, datetime_str=planner.when_param(when), count=4,
            represents=mode)
    except PrimError as e:
        raise HTTPException(502, f"la API de IDFM no responde: {e}")

    options = planner.parse_journeys(data, prefer_latest=(mode == "arrival"))
    if not options:
        raise HTTPException(
            404, "no hay ningún trayecto en transporte público entre esos dos "
                 "puntos a esa hora")
    return {"options": options, "age": round(age, 1)}


@app.post("/api/routes/from-plan")
async def api_route_from_plan(payload: dict):
    """Guarda como ruta vigilada la opcion que se ha elegido."""
    option = payload.get("option") or {}
    if not option.get("legs"):
        raise HTTPException(400, "falta el itinerario elegido")
    data = await planner.route_from_option(option, payload.get("meta") or {})
    route_id = db.save_route(data)

    # Si algun tramo se queda sin direccion es que el texto de Navitia no
    # casaba con el del tiempo real: se avisa en vez de dejarlo en silencio.
    sin_dir = [l["line_code"] for l in data["legs"] if not l["directions"]]
    return {"id": route_id, "route": db.get_route(route_id),
            "without_direction": sin_dir}


@app.get("/api/platform-model")
async def api_platform_model(route_id: int | None = None):
    """Que sabe la prevision del anden y si acierta."""
    out = {"accuracy": await run_in_threadpool(P.accuracy),
           "collector": collector.status()}
    routes = db.list_routes()
    rid = route_id if route_id is not None else (
        (B.pick_active_route(routes) or {}).get("id") if routes else None)
    if rid:
        route = db.get_route(rid)
        if route:
            out["route"] = {"id": route["id"], "name": route["name"]}
            out["coverage"] = await run_in_threadpool(P.coverage, route)
    return out


@app.get("/api/stats")
async def api_stats(days: int = 90):
    return await run_in_threadpool(db.stats, days)


# ---------------- interfaz ----------------

@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """Lo que convierte la pagina en app instalable: nombre, icono y arrancar
    sin barra de navegador."""
    return FileResponse(
        os.path.join(STATIC, "manifest.webmanifest"),
        media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """El service worker MANDA desde la raiz, no desde /static: su alcance es
    la carpeta desde la que se sirve, y desde /static/ no podria responder a
    la portada. Y sin cachear: si el navegador se queda con uno viejo, se
    queda con el para siempre."""
    return FileResponse(
        os.path.join(STATIC, "sw.js"),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"})


# iOS busca estos dos en la raiz cuando el <link> no le vale (por ejemplo al
# guardar un acceso directo desde una pestana ya abierta).
@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_icon():
    return FileResponse(os.path.join(STATIC, "icons", "apple-touch-icon.png"),
                        media_type="image/png")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(os.path.join(STATIC, "icons", "favicon-32.png"),
                        media_type="image/png")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
