"""Trajet - servidor: API del iPhone (/api/v1), panel (/, /api/admin) y la API
de la 0.3.0 (/api) por compatibilidad.

La PWA de usuario de la 0.3.0 ya no existe: la app es la del iPhone.
"""
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logs.setup()
    aplicadas = db.init()
    if aplicadas:
        log.info("migraciones aplicadas: %s", aplicadas)
    await prim.startup()
    collector.start()
    await mapdata.startup()
    log.info("Trajet %s listo. BD en %s", VERSION, settings.db_path)
    yield
    await mapdata.shutdown()
    await collector.stop()
    await prim.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="Trajet", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    errors.install(app)

    # Orden: el ultimo que se anade es el de fuera. La compresion va fuera
    # del ETag para que el hash sea del JSON sin comprimir.
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
