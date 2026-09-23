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


async def translate(text: str) -> str | None:
    """Traduce un aviso. None si no se puede: la pantalla seguira en frances."""
    text = (text or "").strip()
    if not text:
        return None
    hit = cached(text)
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
    store(text, es)
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


def translate_board(board: dict) -> int:
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
        st["messages_es"] = [cached(m) for m in msgs]
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
