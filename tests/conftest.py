"""Fixtures comunes.

Cada test tiene su BD en una carpeta temporal, una clave PRIM de mentira y
un PRIM falso (tests/fakeprim.py). Nada sale a la red.

Fixtures:
  env        entorno limpio (BD temporal, sin recolector, sin mapa, sin Ollama)
  fake_prim  el PRIM falso, ya enchufado al cliente
  app        la app de FastAPI (sin arrancar)
  client     TestClient con la app arrancada (lifespan incluido)
  make_route guarda una ruta de prueba y devuelve su id
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fakeprim import FakePrim  # noqa: E402

# Una clave de mentira con la forma de las reales (32 caracteres). Los tests
# de seguridad comprueban que NUNCA aparece en respuestas, logs ni HTML.
FAKE_KEY = "PRUEBAS0clave0de0mentira0000abcd"


def _reset_module_state():
    """Los modulos guardan cosas en memoria (cache de PRIM, vias vistas, ritmo
    de las estaciones...). Entre tests se vacia todo."""
    from app import board, prim
    from app.api import common
    board._seen_platforms.clear()
    board._next_in.clear()
    common._baseline.clear()
    prim.client = None
    try:
        from app import translate
        translate._en_curso.clear()
        translate._available_cache = None
    except Exception:
        pass
    # Limites de emparejamiento, errores en cola, clave y cuota en memoria.
    from app import auth, db, keystore, logs, planner, quota
    from app import platform as plat
    db.init_error = None                 # modo degradado de un arranque anterior
    auth.reset_state()
    logs.reset_state()
    keystore.reset_store()
    quota.reset_quota()
    planner._coords.clear()
    plat._forget_accuracy()


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("TRAJET_DB", str(data / "trajet.db"))
    monkeypatch.setenv("TRAJET_DATA_DIR", str(data))
    monkeypatch.setenv("PRIM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TRAJET_COLLECT", "0")
    monkeypatch.setenv("TRAJET_MAP", "0")
    monkeypatch.setenv("OLLAMA_URL", "")
    monkeypatch.setenv("TRAJET_SECRET_SEED", "semilla-de-pruebas")
    monkeypatch.setenv("TRAJET_ADMIN_PEERS", "any")
    monkeypatch.delenv("APP_SEED", raising=False)
    from app.config import settings
    settings.reload()
    _reset_module_state()
    yield data
    settings.reload()
    _reset_module_state()


@pytest.fixture
def fake_prim(env):
    from app import prim
    fp = FakePrim()
    prim.transport_override = fp.transport
    yield fp
    prim.transport_override = None


@pytest.fixture
def app(env, fake_prim):
    from app.main import create_app
    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


def ruta_j(**over) -> dict:
    """Casa → Trabajo: la J de Saint-Lazare a Argenteuil (datos reales)."""
    r = {
        "name": "Saint-Lazare → Argenteuil",
        "origin_id": "stop_area:IDFM:71370", "origin_name": "Gare Saint-Lazare",
        "dest_id": "stop_area:IDFM:65063", "dest_name": "Argenteuil",
        "days": [0, 1, 2, 3, 4, 5, 6], "time_from": "00:00", "time_to": "23:59",
        "legs": [{
            "line_id": "line:IDFM:C01739", "line_code": "J", "line_name": "J",
            "line_mode": "Train", "line_color": "CEC73D",
            "from_id": "stop_area:IDFM:71370", "from_name": "Gare Saint-Lazare",
            "to_id": "stop_area:IDFM:65063", "to_name": "Argenteuil",
            "directions": ["Ermont - Eaubonne"],
        }],
    }
    r.update(over)
    return r


@pytest.fixture
def make_route(env):
    from app import db

    def _make(data: dict | None = None) -> int:
        db.init()
        return db.save_route(data or ruta_j())
    return _make
