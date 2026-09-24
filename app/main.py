"""Trajet - servidor: API del iPhone (/api/v1), panel (/, /api/admin) y la API
de la 0.3.0 (/api) por compatibilidad.

La PWA de usuario de la 0.3.0 ya no existe: la app es la del iPhone.
"""
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import db, logs, mapdata, prim
from .api import admin, errors, legacy, v1
from .api.etag import ETagMiddleware, SecurityHeadersMiddleware
from .collector import collector
from .config import VERSION, settings

log = logging.getLogger("trajet")

PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel")
STARTED_AT = time.time()

# Lo que se le dice a quien llama a una API de datos en modo degradado. Sin
# el motivo: /api/v1 responde aqui sin mirar el token y el motivo (un error
# de SQLite) es cosa del panel, que si esta protegido.
DEGRADED_MESSAGE = ("el servidor no puede usar su base de datos: la migración falló al "
                    "arrancar. Mira el panel de Trajet en Umbrel")


def _motivo(e: BaseException) -> str:
    """Motivo corto y sin secretos de un fallo, para el panel."""
    return logs.redact(f"{type(e).__name__}: {e}")[:300]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.setup()
    # Si la migracion falla, el servidor NO se cae: con restart
    # unless-stopped entraria en un bucle de reinicios sin panel ni salud que
    # dijeran por que. Arranca en modo degradado: el panel funciona y ensena
    # el motivo, las APIs de datos responden 503 (DegradedMiddleware) y no se
    # arranca nada que use la BD por su cuenta (recolector, mapa). La
    # migracion va en una transaccion por paso: lo que falle no deja el
    # esquema a medias y el siguiente arranque lo vuelve a intentar.
    db.init_error = None
    try:
        aplicadas = db.init()
    except Exception as e:
        db.init_error = _motivo(e)
        aplicadas = []
        log.exception("la base de datos no se pudo migrar; el servidor arranca en modo "
                      "degradado (APIs de datos con 503) hasta que se arregle")
    if aplicadas:
        log.info("migraciones aplicadas: %s", aplicadas)
    await prim.startup()
    if db.init_error is None:
        collector.start()
        await mapdata.startup()
        log.info("Trajet %s listo. BD en %s", VERSION, settings.db_path)
    else:
        log.error("Trajet %s en modo degradado: sin recolector ni mapa. BD en %s",
                  VERSION, settings.db_path)
    yield
    await mapdata.shutdown()
    await collector.stop()
    await prim.shutdown()


class DegradedMiddleware:
    """Con la BD sin migrar (db.init_error), 503 en las APIs de datos.

    /api/v1/* (salvo /ping, que es el HEALTHCHECK) responde ErrorV1 con
    `internal` y /api/* (0.3.0) `{"detail": ...}`, como el resto de sus
    errores. El panel (/, /panel/static, /api/admin/overview y /errors)
    sigue: es donde se ve el motivo; el resto de /api/admin da 503. Middleware y no dependencia: asi cubre tambien /api/v1/pair y lo
    que aun no tenga ruta, y ninguna ruta llega a tocar un esquema viejo."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _kind(path: str) -> str | None:
        if path == "/api/v1/ping":
            return None
        if path == "/api/v1" or path.startswith("/api/v1/"):
            return "v1"
        if path in ("/api/admin/overview", "/api/admin/errors"):
            return None                  # es donde se ve el motivo
        if path == "/api/admin" or path.startswith("/api/admin/"):
            return "v1"                  # el resto del panel toca la BD: 503
        if path == "/api" or path.startswith("/api/"):
            return "legacy"
        return None

    async def __call__(self, scope, receive, send):
        kind = self._kind(scope.get("path", "")) if scope["type"] == "http" else None
        if kind is None or db.init_error is None:
            await self.app(scope, receive, send)
            return
        if kind == "v1":
            body = errors.error_body("internal", DEGRADED_MESSAGE)
        else:
            body = {"detail": DEGRADED_MESSAGE}
        await JSONResponse(body, status_code=503)(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="Trajet", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    errors.install(app)

    # Orden: el ultimo que se anade es el de fuera. La compresion va fuera
    # del ETag para que el hash sea del JSON sin comprimir. El modo degradado
    # va dentro de todo: sus 503 llevan las cabeceras de seguridad.
    app.add_middleware(DegradedMiddleware)
    app.add_middleware(ETagMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=600)

    app.include_router(v1.router)
    app.include_router(admin.router)
    app.include_router(legacy.router)

    @app.get("/", include_in_schema=False)
    async def panel_index():
        return FileResponse(os.path.join(PANEL, "index.html"),
                            headers={"Cache-Control": "no-cache"})

    static_dir = os.path.join(PANEL, "static")
    if os.path.isdir(static_dir):
        app.mount("/panel/static", StaticFiles(directory=static_dir), name="panel")
    return app


app = create_app()
