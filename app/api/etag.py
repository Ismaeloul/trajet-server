"""ETag debil y 304 para las respuestas JSON de la API del iPhone.

Middleware ASGI puro: solo actua en GET de /api/v1/* con respuesta 200 JSON.
Guarda el cuerpo (son pocos KB), calcula `W/"<hash>"` y, si coincide con
`If-None-Match`, responde 304 sin cuerpo. Va por DENTRO de la compresion,
asi que el hash es del JSON sin comprimir.
"""
from __future__ import annotations

import hashlib

from starlette.datastructures import Headers, MutableHeaders

import re

# Solo donde el contrato declara 304: el tablero, la lista de rutas y el mapa.
# El resto de /api/v1 no gana nada con un ETag (buscadores, salud...).
PREFIXES = ("/api/v1/",)
_ETAG_PATHS = re.compile(r"^/api/v1/(board|routes|routes/\d+/map)$")
MAX_BODY = 2 * 1024 * 1024


def etag_for(body: bytes) -> str:
    return 'W/"' + hashlib.sha256(body).hexdigest()[:24] + '"'


def _matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    wanted = {t.strip() for t in if_none_match.split(",")}
    bare = etag[2:] if etag.startswith("W/") else etag
    return etag in wanted or bare in wanted or ("W/" + bare) in wanted


class ETagMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "GET"
                or not _ETAG_PATHS.match(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        if_none_match = Headers(scope=scope).get("if-none-match")
        start: dict | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def wrapped_send(message):
            nonlocal start, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = Headers(raw=message.get("headers", []))
                ctype = headers.get("content-type", "")
                if message["status"] != 200 or "json" not in ctype:
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    if sum(len(c) for c in chunks) > MAX_BODY:
                        # Demasiado grande para merecer ETag: se suelta tal cual.
                        passthrough = True
                        await send(start)
                        await send({"type": "http.response.body",
                                    "body": b"".join(chunks), "more_body": True})
                    return
                body = b"".join(chunks)
                etag = etag_for(body)
                headers = MutableHeaders(raw=list(start.get("headers", [])))
                headers["ETag"] = etag
                if _matches(if_none_match, etag):
                    del headers["content-length"]
                    if "content-type" in headers:
                        del headers["content-type"]
                    await send({"type": "http.response.start", "status": 304,
                                "headers": headers.raw})
                    await send({"type": "http.response.body", "body": b""})
                    return
                await send({"type": "http.response.start", "status": 200,
                            "headers": headers.raw})
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        await self.app(scope, receive, wrapped_send)


class SecurityHeadersMiddleware:
    """Cabeceras de seguridad y de cache.

    - Todo: nosniff, sin referrer, sin iframes.
    - Panel (HTML): CSP estricta, solo recursos propios.
    - /api/admin: no-store (lleva datos del servidor que no deben quedarse
      en ninguna cache).
    """

    CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
           "script-src 'self'; connect-src 'self'; font-src 'self'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=list(message.get("headers", [])))
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("X-Frame-Options", "DENY")
                ctype = headers.get("content-type", "")
                if "text/html" in ctype:
                    headers.setdefault("Content-Security-Policy", self.CSP)
                if path.startswith("/api/admin"):
                    headers["Cache-Control"] = "no-store"
                elif path.startswith("/panel/static/"):
                    # Tras actualizar la app, el navegador no debe quedarse con
                    # el JS o el CSS viejos: revalida siempre (ETag de Starlette).
                    headers["Cache-Control"] = "no-cache"
                elif path.startswith("/api/v1/") and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-cache"
                message = dict(message)
                message["headers"] = headers.raw
            await send(message)

        await self.app(scope, receive, wrapped_send)
