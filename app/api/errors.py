"""Errores de la API.

- /api/* (0.3.0): se conserva el formato de FastAPI, `{"detail": "..."}`.
- /api/v1/* y /api/admin/*: sobre `{"error": {"code", "message"}}` (ErrorV1
  en docs/openapi.yaml). `code` es estable; la app lo usa para enseñar un
  estado diseñado en vez de un error generico.
"""
from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# code -> estado HTTP por defecto
STATUS = {
    "bad_request": 400,
    "unauthorized": 401,
    "pairing_invalid": 401,
    "forbidden": 403,
    "not_found": 404,
    "prim_key_rejected": 422,
    "rate_limited": 429,
    "internal": 500,
    "upstream": 502,
    "prim_unreachable": 502,
    "prim_key_missing": 503,
    "prim_key_invalid": 503,
    "prim_quota_exhausted": 503,
}


class ApiError(Exception):
    """Error con codigo estable. Se traduce a ErrorV1 en /api/v1 y /api/admin
    y a `{"detail": message}` en las rutas de la 0.3.0."""

    def __init__(self, code: str, message: str, status: int | None = None,
                 retry_after: int | None = None, headers: dict | None = None,
                 legacy: tuple[int, str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status or STATUS.get(code, 400)
        self.retry_after = retry_after
        self.headers = dict(headers or {})
        if retry_after is not None:
            self.headers.setdefault("Retry-After", str(int(retry_after)))
        # (estado, texto) con que responde la API de la 0.3.0 cuando no es el
        # mismo que el de la v1 (los fallos de PRIM: alli siempre 502).
        self.legacy = legacy


def is_v2_path(path: str) -> bool:
    return path.startswith("/api/v1/") or path.startswith("/api/admin/") \
        or path in ("/api/v1", "/api/admin")


def error_body(code: str, message: str, retry_after: int | None = None) -> dict:
    err = {"code": code, "message": message}
    if retry_after is not None:
        err["retry_after"] = int(retry_after)
    return {"error": err}


def _v2_code_for_status(status: int) -> str:
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden",
            404: "not_found", 405: "bad_request", 422: "bad_request",
            429: "rate_limited", 502: "upstream", 503: "prim_unreachable"
            }.get(status, "internal" if status >= 500 else "bad_request")


async def api_error_handler(request: Request, exc: ApiError):
    if is_v2_path(request.url.path):
        return JSONResponse(error_body(exc.code, exc.message, exc.retry_after),
                            status_code=exc.status, headers=exc.headers)
    return JSONResponse({"detail": exc.message}, status_code=exc.status,
                        headers=exc.headers)


async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if is_v2_path(request.url.path):
        detail = exc.detail if isinstance(exc.detail, str) else "error"
        return JSONResponse(error_body(_v2_code_for_status(exc.status_code), detail),
                            status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


async def validation_error_handler(request: Request, exc: RequestValidationError):
    if is_v2_path(request.url.path):
        # Mensaje corto y en espanol; sin eco de lo que se mando (podria ser
        # un codigo o una clave).
        campos = []
        for e in exc.errors():
            loc = [str(x) for x in e.get("loc", []) if x not in ("body", "query", "path", "header")]
            if loc:
                campos.append(".".join(loc))
        msg = "datos no validos" + (f": {', '.join(sorted(set(campos)))}" if campos else "")
        return JSONResponse(error_body("bad_request", msg), status_code=400)
    from fastapi.exception_handlers import request_validation_exception_handler
    return await request_validation_exception_handler(request, exc)


async def unhandled_error_handler(request: Request, exc: Exception):
    import logging
    logging.getLogger("trajet").exception("error no controlado en %s", request.url.path)
    if is_v2_path(request.url.path):
        return JSONResponse(error_body("internal", "error interno del servidor"),
                            status_code=500)
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


def install(app) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
