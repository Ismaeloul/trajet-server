"""Traduccion de los avisos con Ollama (app/translate.py): R8 y R80.

El frances nunca se pierde; se traduce entero; una vez por texto (cache en la
BD); el tablero nunca espera al modelo. Ollama es un httpx.MockTransport:
nada sale a la red.
"""
from __future__ import annotations

import asyncio
import json
import time
import types

import httpx
import pytest

from app import translate as T
from app.config import settings

AVISO = ("Trafic interrompu entre Saint-Lazare et Houilles en raison d'un malaise "
         "voyageur. Reprise estimée à 18h30.")
AVISO_ES = ("Tráfico interrumpido entre Saint-Lazare y Houilles por un malestar de un "
            "viajero. Reanudación estimada a las 18:30.")


@pytest.fixture
def bd(env):
    from app import db
    db.init()
    return env


class Ollama:
    """Ollama falso: cuenta peticiones y contesta lo que se le diga."""

    def __init__(self, response: str = AVISO_ES, models=("gemma3:4b",)):
        self.response = response
        self.models = list(models)
        self.generate: list[dict] = []
        self.tags = 0
        self.fail: str | int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail == "down":
            raise httpx.ConnectError("no responde", request=request)
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, json={"error": "fallo"})
        if request.url.path == "/api/tags":
            self.tags += 1
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if request.url.path == "/api/generate":
            self.generate.append(json.loads(request.content))
            return httpx.Response(200, json={"response": self.response})
        return httpx.Response(404)


@pytest.fixture
def ollama(bd, monkeypatch):
    """Enchufa el Ollama falso solo en translate.py (no en todo httpx)."""
    fake = Ollama()
    real = httpx.AsyncClient

    def cliente(**kw):
        return real(transport=httpx.MockTransport(fake.handler), **kw)

    monkeypatch.setattr(T, "httpx", types.SimpleNamespace(AsyncClient=cliente))
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid:11434")
    monkeypatch.setattr(settings, "ollama_model", "gemma3:4b")
    return fake


def _tablero(*msgs: str) -> dict:
    return {"legs": [{"status": {"level": 1, "label": "perturbada", "messages": list(msgs),
                                 "planned": 0}},
                     {"status": {"level": 0, "label": "normal", "messages": [], "planned": 0}}]}


async def _esperar_traducciones():
    tareas = list(T._en_curso.values())
    await asyncio.gather(*tareas, return_exceptions=True)


# ---------------- R80: entero, una vez, sin perder el frances ----------------

async def test_traduccion_entera_una_vez_por_texto(ollama):
    """R80: el prompt pide traducirlo ENTERO y va el aviso completo; la
    segunda vez sale de la cache sin llamar al modelo."""
    assert await T.translate(AVISO) == AVISO_ES
    assert len(ollama.generate) == 1
    peticion = ollama.generate[0]
    assert peticion["prompt"] == T.PROMPT + AVISO
    assert "ENTERO" in T.PROMPT and "no resumas" in T.PROMPT
    assert peticion["model"] == "gemma3:4b" and peticion["stream"] is False
    assert peticion["options"]["temperature"] == 0.1
    assert await T.translate(AVISO) == AVISO_ES
    assert await T.translate("  " + AVISO + "  ") == AVISO_ES
    assert len(ollama.generate) == 1                       # una vez por texto
    assert T.cached(AVISO) == AVISO_ES
    assert await T.translate("") is None and await T.translate(None) is None


async def test_fallo_de_ollama_deja_el_frances_y_no_se_recuerda(ollama):
    """Sin traduccion el aviso sigue en frances; un fallo no se guarda, asi
    que el siguiente refresco lo vuelve a intentar."""
    ollama.fail = 500
    assert await T.translate(AVISO) is None
    ollama.fail = "down"
    assert await T.translate(AVISO) is None
    ollama.fail = None
    ollama.response = "   "
    assert await T.translate(AVISO) is None                # respuesta vacia
    assert T.cached(AVISO) is None
    ollama.response = AVISO_ES
    assert await T.translate(AVISO) == AVISO_ES


async def test_sin_ollama_configurado_no_se_llama(ollama, monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "")
    assert await T.translate(AVISO) is None and ollama.generate == []
    board = _tablero(AVISO)
    T.translate_board(board)
    st = board["legs"][0]["status"]
    assert st["messages_es"] == [None] and st["translating"] is False
    assert T._en_curso == {}


async def test_board_no_espera_traduccion(ollama, monkeypatch):
    """R8 y R80: el tablero sale YA con el frances y «traduciendo»; la
    traduccion se encarga una sola vez en segundo plano y aparece en el
    refresco siguiente. El original no se pierde nunca."""
    soltar = asyncio.Event()
    real = T.translate

    async def lento(text):
        await soltar.wait()
        return await real(text)

    monkeypatch.setattr(T, "translate", lento)
    board = _tablero(AVISO, "Trafic perturbé.")
    t0 = time.monotonic()
    assert await T.translate_board_async(board) == 0
    assert time.monotonic() - t0 < 1
    st = board["legs"][0]["status"]
    assert st["messages"] == [AVISO, "Trafic perturbé."]
    assert st["messages_es"] == [None, None] and st["translating"] is True
    assert "messages_es" not in board["legs"][1]["status"]     # sin avisos, nada
    assert len(T._en_curso) == 2

    # Otro refresco mientras traduce: no se encarga dos veces lo mismo.
    T.translate_board(_tablero(AVISO))
    assert len(T._en_curso) == 2

    soltar.set()
    await _esperar_traducciones()
    assert T._en_curso == {}
    board = _tablero(AVISO)
    assert await T.translate_board_async(board) == 1
    st = board["legs"][0]["status"]
    assert st["messages"] == [AVISO] and st["messages_es"] == [AVISO_ES]
    assert st["translating"] is False


def test_board_no_espera_traduccion_por_la_api(client, fake_prim, make_route, monkeypatch):
    """Lo mismo de punta a punta: /api/board con un Ollama que no contesta
    nunca responde al momento, en frances."""
    from fakeprim import Dep, Msg

    async def nunca(text):
        await asyncio.sleep(3600)

    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid:11434")
    monkeypatch.setattr(T, "translate", nunca)
    make_route()
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4))
    fake_prim.message(Msg(["C01739"], AVISO))
    t0 = time.monotonic()
    st = client.get("/api/board").json()["legs"][0]["status"]
    assert time.monotonic() - t0 < 3
    assert st["messages"] == [AVISO] and st["messages_es"] == [None]
    assert st["translating"] is True

    async def cancelar():
        for t in list(T._en_curso.values()):
            t.cancel()
        await asyncio.gather(*T._en_curso.values(), return_exceptions=True)

    client.portal.call(cancelar)


async def test_cache_de_la_bd_se_lee_en_un_hilo(ollama, monkeypatch):
    """§18.4: la cache de traducciones (SQLite) no se lee en el bucle."""
    import threading
    bucle = threading.get_ident()
    hilos = []
    real = T.cached_many

    def contado(texts):
        hilos.append(threading.get_ident())
        return real(texts)

    monkeypatch.setattr(T, "cached_many", contado)
    T.store(AVISO, AVISO_ES)
    board = _tablero(AVISO, AVISO)
    assert await T.translate_board_async(board) == 2
    assert hilos and bucle not in hilos
    assert T.cached_many([]) == {}


# ---------------- disponibilidad ----------------

async def test_available(ollama, monkeypatch):
    ok = await T.available()
    assert ok == {"ok": True, "reason": "", "model": "gemma3:4b", "models": ["gemma3:4b"]}
    # Vale el mismo modelo con otra etiqueta (la parte antes de «:»).
    ollama.models = ["gemma3:12b"]
    assert (await T.available())["ok"] is True
    ollama.models = ["llama3:8b"]
    falta = await T.available()
    assert falta["ok"] is False and falta["reason"] == "falta el modelo gemma3:4b"
    ollama.fail = "down"
    caido = await T.available()
    assert caido["ok"] is False and caido["reason"].startswith("no responde")
    assert caido["models"] == []
    monkeypatch.setattr(settings, "ollama_url", "")
    assert await T.available() == {"ok": False, "reason": "sin configurar"}


async def test_available_cached_no_espera_a_ollama_en_cada_health(ollama, monkeypatch):
    """/api/health lo pregunta cada 30 s (HEALTHCHECK): una vez por minuto."""
    T._available_cache = None
    a = await T.available_cached()
    b = await T.available_cached()
    assert a == b and a["ok"] is True and ollama.tags == 1
    b["ok"] = False                                         # es una copia
    assert (await T.available_cached())["ok"] is True
    monkeypatch.setattr(T, "AVAILABLE_TTL", 0.0)
    await T.available_cached()
    assert ollama.tags == 2
