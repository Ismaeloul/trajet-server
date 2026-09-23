"""Pruebas contra la API REAL de PRIM: gastan cuota de verdad.

No van en la tanda normal (pyproject.toml ignora tests/real). Se lanzan con
scripts/test-real.sh, que fija TRAJET_QUOTA_CAP=800.

  - Todo lleva la marca `real`.
  - La clave sale de PRIM_API_KEY o de trajet-server/.env. Nunca se imprime:
    va envuelta en `Clave`, cuyo repr es '***' (pytest ensena los valores de
    un assert que falla). Sin clave, se salta todo.
  - Cada llamada real pasa por un transporte que la apunta ANTES de hacerla
    en trajet-server/.local/real-quota-<AAAA-MM-DD>.json (dia UTC, por
    endpoint). Al llegar a 800 en un endpoint, lo que falte se salta: la
    cuota de verdad es de 1000 al dia y el servidor de casa tambien la usa.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOCAL = os.path.join(ROOT, ".local")
TOPE = 800


class Clave(str):
    """La clave real, con un repr que no la ensena."""

    def __repr__(self) -> str:
        return "'***'"


def leer_clave() -> str:
    """PRIM_API_KEY del entorno o, si no, de trajet-server/.env."""
    valor = os.environ.get("PRIM_API_KEY", "").strip()
    if valor:
        return valor
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
            for linea in f:
                linea = linea.strip()
                if linea.startswith("export "):
                    linea = linea[len("export "):].strip()
                if linea.startswith("PRIM_API_KEY="):
                    return linea.split("=", 1)[1].strip().strip("'\"")
    except FileNotFoundError:
        pass
    return ""


def endpoint_de(path: str) -> str:
    """El cubo de cuota de una ruta de PRIM (igual que prim.py y fakeprim.py)."""
    if path.startswith("/marketplace/v2/navitia"):
        return "navitia"
    if path.endswith("/stop-monitoring"):
        return "stop-monitoring"
    if path.endswith("/general-message"):
        return "general-message"
    return "otro"


class Contador:
    """Llamadas reales por endpoint y dia UTC, persistidas entre ejecuciones."""

    def __init__(self, tope: int = TOPE, carpeta: str = LOCAL):
        self.tope = tope
        self.carpeta = carpeta
        self._lock = threading.Lock()

    def ruta(self) -> str:
        dia = datetime.now(timezone.utc).date().isoformat()
        return os.path.join(self.carpeta, f"real-quota-{dia}.json")

    def leer(self) -> dict[str, int]:
        try:
            with open(self.ruta(), encoding="utf-8") as f:
                return {k: int(v) for k, v in json.load(f).items()}
        except (FileNotFoundError, ValueError):
            return {}

    def quedan(self, endpoint: str) -> int:
        return max(0, self.tope - self.leer().get(endpoint, 0))

    def reservar(self, endpoint: str) -> None:
        """Apunta una llamada ANTES de hacerla; si no queda, se salta el test."""
        with self._lock:
            cuenta = self.leer()
            hechas = cuenta.get(endpoint, 0)
            if hechas >= self.tope:
                pytest.skip(f"tope de {self.tope} llamadas reales a {endpoint} hoy (UTC)")
            cuenta[endpoint] = hechas + 1
            os.makedirs(self.carpeta, exist_ok=True)
            tmp = self.ruta() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cuenta, f, indent=1, sort_keys=True)
            os.replace(tmp, self.ruta())


class TransporteContado(httpx.AsyncBaseTransport):
    """La red de verdad, pero cada peticion pasa antes por el contador."""

    def __init__(self, contador: Contador):
        self.contador = contador
        self.hechas: list[str] = []
        self._red = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        ep = endpoint_de(request.url.path)
        self.contador.reservar(ep)
        self.hechas.append(ep)
        return await self._red.handle_async_request(request)

    async def aclose(self) -> None:
        # Lo comparten el cliente de prim.py y los temporales de validate_key;
        # que uno se cierre no puede cerrar la red de los demas.
        pass

    async def cerrar(self) -> None:
        await self._red.aclose()


def pytest_collection_modifyitems(config, items):
    for item in items:
        if str(item.fspath).startswith(HERE):
            item.add_marker(pytest.mark.real)


@pytest.fixture(scope="session")
def clave_real() -> Clave:
    clave = leer_clave()
    if not clave:
        pytest.skip("sin PRIM_API_KEY (ni en el entorno ni en .env): no hay pruebas reales")
    return Clave(clave)


@pytest.fixture(scope="session")
def contador() -> Contador:
    return Contador()


@pytest.fixture
async def servidor(tmp_path, monkeypatch, contador):
    """El servidor con BD y secretos en una carpeta temporal y la red contada.

    `await servidor.arrancar(clave_entorno)` arranca prim.py con esa clave en
    PRIM_API_KEY (vacia = sin clave de entorno) y devuelve el cliente.
    """
    from app import db, prim
    from app.config import settings

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("TRAJET_DB", str(data / "trajet.db"))
    monkeypatch.setenv("TRAJET_DATA_DIR", str(data))
    monkeypatch.setenv("TRAJET_COLLECT", "0")
    monkeypatch.setenv("TRAJET_MAP", "0")
    monkeypatch.setenv("OLLAMA_URL", "")
    monkeypatch.setenv("TRAJET_SECRET_SEED", "semilla-de-las-pruebas-reales")
    monkeypatch.setenv("TRAJET_QUOTA_CAP", os.environ.get("TRAJET_QUOTA_CAP") or str(TOPE))
    transporte = TransporteContado(contador)
    monkeypatch.setattr(prim, "transport_override", transporte)

    async def arrancar(clave_entorno: str):
        monkeypatch.setenv("PRIM_API_KEY", str(clave_entorno))
        settings.reload()
        db.init()
        await prim.startup()
        return prim.get_client()

    try:
        yield SimpleNamespace(arrancar=arrancar, transporte=transporte, data=data)
    finally:
        await prim.shutdown()
        prim.client = None
        await transporte.cerrar()
