"""Traduccion de los avisos de perturbacion al espanol, con el LLM local.

Aqui SI tiene sentido un modelo de lenguaje: los avisos de IDFM son texto
libre en frances, escrito por personas, y dicen cosas como "en raison d'un
malaise voyageur" o "suite a une avarie de signalisation". Eso no se traduce
con una tabla, y ademas es la parte interesante: en Francia detallan que ha
pasado de verdad.

Reglas que se respetan:

  - El texto original NUNCA se pierde: la traduccion se anade al lado. Si el
    modelo se equivoca, el frances sigue ahi para comprobarlo.
  - Se traduce entero, sin resumir. Lo que interesa es el detalle.
  - Todo va en cache en la base de datos por hash del original: un aviso se
    traduce una vez y ya. Los avisos duran horas o dias.
  - Si Ollama no responde, no hay modelo, o tarda demasiado, la pantalla sale
    igual con el frances. La traduccion es un extra, nunca un requisito.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

import httpx

from . import db
from .config import settings

log = logging.getLogger("trajet.translate")

PROMPT = (
    "Traduce al español este aviso del transporte público de París. "
    "Tradúcelo ENTERO y con todo el detalle: no resumas, no omitas causas, "
    "horas, nombres de estaciones ni de líneas. Los nombres propios de "
    "estaciones y líneas se dejan en francés tal cual. "
    "Responde SOLO con la traducción, sin comillas ni comentarios.\n\n"
)

# Un aviso son unas pocas frases; si tarda mas que esto, no merece la pena
# hacer esperar a la pantalla.
TIMEOUT = 45.0


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def cached(text: str) -> str | None:
    with db.conn() as c:
        r = c.execute("SELECT es FROM translations WHERE k = ?",
                      (_key(text),)).fetchone()
    return r["es"] if r else None


def store(text: str, es: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO translations (k, fr, es, model, ts) "
            "VALUES (?,?,?,?,datetime('now'))",
            (_key(text), text, es, settings.ollama_model))


async def available() -> dict:
    """Que hay al otro lado, para poder decirlo en pantalla sin adivinar."""
    if not settings.ollama_url:
        return {"ok": False, "reason": "sin configurar"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as cli:
            r = await cli.get(f"{settings.ollama_url}/api/tags")
            r.raise_for_status()
            modelos = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:
        return {"ok": False, "reason": f"no responde: {e}", "models": []}

    quiere = settings.ollama_model
    tiene = any(m == quiere or m.split(":")[0] == quiere.split(":")[0]
                for m in modelos)
    return {
        "ok": tiene,
        "reason": "" if tiene else f"falta el modelo {quiere}",
        "model": quiere,
        "models": modelos,
    }


_available_cache: tuple[float, dict] | None = None
AVAILABLE_TTL = 60.0


async def available_cached() -> dict:
    """available() guardado 60 s: /api/health lo llama el HEALTHCHECK cada 30 s
    y no debe esperar 5 s a Ollama cada vez."""
    import time
    global _available_cache
    now = time.monotonic()
    if _available_cache and now - _available_cache[0] < AVAILABLE_TTL:
        return dict(_available_cache[1])
    res = await available()
    _available_cache = (now, res)
    return dict(res)


async def translate(text: str) -> str | None:
    """Traduce un aviso. None si no se puede: la pantalla seguira en frances."""
    text = (text or "").strip()
    if not text:
        return None
    # Corre como tarea de fondo en el bucle de eventos: la cache (SQLite) se
    # lee y se escribe en un hilo para no frenar las peticiones del iPhone.
    hit = await asyncio.to_thread(cached, text)
    if hit is not None:
        return hit
    if not settings.ollama_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cli:
            r = await cli.post(
                f"{settings.ollama_url}/api/generate",
                json={
                    "model": settings.ollama_model,
                    "prompt": PROMPT + text,
                    "stream": False,
                    # El NAS va justo de memoria (menos de 1 GB libre), asi
                    # que el modelo se descarga en cuanto termina en vez de
                    # quedarse residente. Se traduce de tarde en tarde: solo
                    # los avisos nuevos.
                    "keep_alive": "30s",
                    # Temperatura baja: se quiere una traduccion fiel, no una
                    # redaccion bonita.
                    "options": {"temperature": 0.1, "num_predict": 600},
                })
            r.raise_for_status()
            es = (r.json().get("response") or "").strip()
    except Exception as e:
        log.warning("no se pudo traducir: %s", e)
        return None

    if not es:
        return None
    await asyncio.to_thread(store, text, es)
    return es


# Traducciones en marcha, para no pedir dos veces la misma mientras la
# pantalla refresca cada 30 s. Se guarda la TAREA, no solo el hash: el bucle
# de eventos solo conserva referencias debiles a sus tareas, y una sin
# referencia fuerte puede desaparecer a medio ejecutar.
_en_curso: dict[str, asyncio.Task] = {}


async def _background(text: str) -> None:
    try:
        await translate(text)
    finally:
        _en_curso.pop(_key(text), None)


def cached_many(texts: list[str]) -> dict[str, str | None]:
    """Varias traducciones de golpe, con una sola conexion a la BD."""
    out: dict[str, str | None] = {}
    if not texts:
        return out
    with db.conn() as c:
        for t in texts:
            if t in out:
                continue
            r = c.execute("SELECT es FROM translations WHERE k = ?",
                          (_key(t),)).fetchone()
            out[t] = r["es"] if r else None
    return out


async def translate_board_async(board: dict) -> int:
    """Como translate_board, pero las lecturas de SQLite van a un hilo aparte
    para no bloquear el bucle de eventos."""
    from fastapi.concurrency import run_in_threadpool

    msgs = [m for leg in board.get("legs", [])
            for m in ((leg.get("status") or {}).get("messages") or [])]
    cache = await run_in_threadpool(cached_many, msgs)
    return translate_board(board, cache)


def translate_board(board: dict, cache: dict[str, str | None] | None = None) -> int:
    """Pone las traducciones que ya hay y lanza las que faltan en segundo plano.

    NO espera al modelo. Un aviso nuevo tarda unos 17 s en gemma3:4b (carga
    del modelo incluida) y el tablero no puede quedarse colgado ese rato
    estando yo de pie en un anden. La pantalla sale ya con el frances, y en
    el refresco siguiente (30 s) aparece traducido.
    """
    puestos = 0
    for leg in board.get("legs", []):
        st = leg.get("status") or {}
        msgs = st.get("messages", [])
        if not msgs:
            continue
        st["messages_es"] = [cache[m] if cache is not None and m in cache
                             else cached(m) for m in msgs]
        puestos += sum(1 for x in st["messages_es"] if x)

        for msg, es in zip(msgs, st["messages_es"]):
            if es is not None or not settings.ollama_url:
                continue
            k = _key(msg)
            if k in _en_curso:
                continue
            _en_curso[k] = asyncio.create_task(_background(msg))

        # Para poder decirlo en pantalla: hay algo pendiente y el modelo esta
        # en ello, asi que aparecera en el refresco siguiente. Distinto de "no
        # hay traductor", donde el frances se queda para siempre.
        st["translating"] = bool(settings.ollama_url) and any(
            x is None for x in st["messages_es"])

    return puestos
