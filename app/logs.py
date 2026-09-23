"""Logs sin secretos y registro de errores recientes para el panel.

Tres cosas:

  - RedactingFilter tacha con *** los secretos antes de que ningun handler
    escriba nada: la clave PRIM en uso y la del entorno (register_secret),
    cualquier `Bearer <x>`, los tokens `trj_…`, `apikey=…` y los codigos de
    emparejamiento `XXXX-XXXX`. Formatea primero el mensaje con sus
    argumentos (un secreto puede venir en un argumento) y tacha tambien las
    excepciones.
  - El log de acceso de uvicorn se queda sin query strings: una peticion
    /api/v1/plan?from=<lon;lat de casa> no deja las coordenadas de casa en
    los logs del contenedor.
  - WARNING o peor va tambien a la tabla error_log (los 500 ultimos), ya
    tachado, para ensenarlo en el panel. Si la BD aun no existe se guarda en
    memoria y se escribe en cuanto se pueda.

El filtro se pone en el logger raiz, en los de uvicorn y en sus handlers, y
ademas en la fabrica de registros de logging: asi cubre tambien los loggers
hijos (trajet.prim…) y cualquier handler que se anada despues.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote

from .config import settings

MASK = "***"
KEEP_ERRORS = 500
MAX_MESSAGE = 4000
_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Patrones que se tachan aunque nadie los haya registrado.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Authorization: Bearer <lo que sea>
    (re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer " + MASK),
    # Tokens de dispositivo, aunque vengan sueltos o cortados.
    (re.compile(r"trj_[A-Za-z0-9_-]+"), "trj_" + MASK),
    # apikey=…, PRIM_API_KEY=…, 'apikey': '…', api-key: …
    (re.compile(r"(api[_-]?key\w*[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;)}\]]+", re.IGNORECASE),
     r"\1" + MASK),
    # Codigo de emparejamiento como valor de un campo `code` (en minusculas,
    # sin guion o con espacio: como lo teclee la app).
    (re.compile(r"(\bcode[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9]{4}[\s-]?[A-Za-z0-9]{4}\b",
                re.IGNORECASE), r"\1" + MASK),
    # Codigo de emparejamiento suelto: ABCD-EFGH con el alfabeto sin 0/O/1/I.
    (re.compile(r"\b[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}\b"), MASK),
]

_secrets: list[str] = []
_secrets_re: re.Pattern | None = None
_secrets_lock = threading.Lock()
_MIN_SECRET = 6
_MAX_SECRETS = 32


def register_secret(value: str | None) -> None:
    """Apunta un secreto (p. ej. la clave PRIM en uso) para tacharlo de
    cualquier log. Lo llama prim.py al arrancar y al cambiar de clave; la
    del entorno la apunta setup(). Se pueden registrar varios y no se
    olvidan: una clave vieja tampoco debe aparecer."""
    global _secrets_re
    if not isinstance(value, str):
        return
    value = value.strip()
    if len(value) < _MIN_SECRET:          # uno muy corto tacharia medio log
        return
    variants = {value, quote(value, safe="")}
    with _secrets_lock:
        for v in variants:
            if v not in _secrets:
                _secrets.append(v)
        del _secrets[:-_MAX_SECRETS]
        # Los largos primero: si uno contiene a otro, se tacha entero.
        ordered = sorted(_secrets, key=len, reverse=True)
        _secrets_re = re.compile("|".join(re.escape(s) for s in ordered))


def redact(text) -> str:
    """El texto con los secretos tachados."""
    if not isinstance(text, str):
        text = str(text)
    pat = _secrets_re
    if pat is not None:
        text = pat.sub(MASK, text)
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


def strip_query(path) -> str:
    """/api/v1/plan?from=… -> /api/v1/plan"""
    if not isinstance(path, str):
        return path
    return path.split("?", 1)[0]


# httpx apunta cada peticion a PRIM con la URL entera («HTTP Request: GET
# …/journeys?from=<lon;lat de casa>…»): mismo problema que el log de acceso.
_URL_QUERY = re.compile(r"(\bhttps?://[^\s?#'\"]+)\?[^\s#'\"]*")
_QUERY_LOGGERS = ("httpx", "httpcore")


class RedactingFilter(logging.Filter):
    """Tacha los secretos del registro en el sitio. Nunca descarta nada."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if not getattr(record, "_trajet_redacted", False):
                _redact_record(record)
            elif ("color_message" in record.__dict__
                  and not record.__dict__.get("_trajet_color", False)):
                # Llego con `extra` despues de la fabrica de registros (que ya
                # habia gastado los argumentos): se pinta sin color pero con
                # el texto ya tachado.
                record.color_message = record.msg
                record._trajet_color = True
        except Exception:
            # Un log mal formado no debe romper nada, pero tampoco salir sin
            # tachar: se sustituye entero (falla cerrado).
            _hide(record)
        record._trajet_redacted = True
        return True


def _hide(record: logging.LogRecord) -> None:
    record.msg = "[registro oculto: no se pudo tachar]"
    if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) == 5:
        record.args = ("-", "-", "-", "-", 0)
    else:
        record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    record.__dict__.pop("color_message", None)


def _redact_record(record: logging.LogRecord) -> None:
    args = record.args
    if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
        # El AccessFormatter de uvicorn desempaqueta estos 5 argumentos: hay
        # que dejar la tupla con la misma forma. Fuera la query string.
        client, method, path, version, status = args
        record.args = (client, method, redact(strip_query(path)), version, status)
        record.msg = redact(record.msg) if isinstance(record.msg, str) else record.msg
    else:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        color = record.__dict__.get("color_message")
        if isinstance(color, str):
            # uvicorn pinta a color con este mensaje alternativo (y los
            # mismos argumentos): se tacha igual o se quita.
            try:
                record.color_message = redact(color % args if args else color)
            except Exception:
                del record.__dict__["color_message"]
            record._trajet_color = True
        if record.name.split(".", 1)[0] in _QUERY_LOGGERS:
            message = _URL_QUERY.sub(r"\1", message)
        record.msg = redact(message)
        record.args = ()
    if record.exc_info and record.exc_info[0] is not None:
        text = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
        record.exc_text = redact(text)
        # Sin exc_info: un formateador que volviera a pintar la excepcion
        # desde el objeto se saltaria el tachado.
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)


_FILTER = RedactingFilter()


def _install_record_factory() -> None:
    current = logging.getLogRecordFactory()
    if getattr(current, "_trajet_redacting", False):
        return

    def factory(*args, **kwargs):
        record = current(*args, **kwargs)
        _FILTER.filter(record)
        return record

    factory._trajet_redacting = True
    logging.setLogRecordFactory(factory)


# ---------------- errores recientes (tabla error_log) ----------------

def _level_name(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "CRITICAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    return "WARNING"


class ErrorLogHandler(logging.Handler):
    """WARNING o peor a la tabla error_log, ya tachado.

    No usa db.conn() a proposito: un log no puede crear la BD antes de las
    migraciones ni esperar 10 s a un bloqueo. Si la BD o la tabla aun no
    existen (o esta ocupada), se guarda en memoria y se escribe con el
    siguiente. Nunca registra nada desde dentro (sin recursion)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.pending: deque = deque(maxlen=KEEP_ERRORS)
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False) or getattr(record, "_trajet_stored", False):
            return
        self._local.busy = True
        try:
            record._trajet_stored = True
            _FILTER.filter(record)
            message = redact(record.getMessage())
            if record.exc_text:
                message = f"{message}\n{redact(record.exc_text)}"
            if len(message) > MAX_MESSAGE:
                message = message[:MAX_MESSAGE] + " […]"
            ts = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds")
            self.pending.append((ts, _level_name(record.levelno), record.name, message))
            self.flush_pending()
        except Exception:  # noqa: S110
            # Ni propagar ni registrar (seria recursivo) ni handleError (lo
            # pinta en stderr en cada aviso mientras la BD este ocupada): lo
            # que no se pudo escribir sigue en `pending`.
            pass
        finally:
            self._local.busy = False

    def flush_pending(self) -> bool:
        if not self.pending:
            return True
        path = settings.db_path
        if not path or not os.path.exists(path):
            return False
        items = list(self.pending)
        try:
            con = sqlite3.connect(path, timeout=0.5)
        except sqlite3.Error:
            return False
        try:
            con.executemany("INSERT INTO error_log (ts, level, logger, message) "
                            "VALUES (?,?,?,?)", items)
            con.execute("DELETE FROM error_log WHERE id <= "
                        "(SELECT MAX(id) FROM error_log) - ?", (KEEP_ERRORS,))
            con.commit()
        except sqlite3.Error:
            return False
        finally:
            con.close()
        for _ in items:
            if self.pending:
                self.pending.popleft()
        return True


_ERROR_HANDLER = ErrorLogHandler()


def recent_errors(limit: int = 50) -> list[dict]:
    """ErrorLogEntry del contrato, del mas nuevo al mas viejo (1..200).

    Lee SQLite: desde una ruta, con run_in_threadpool."""
    limit = max(1, min(int(limit), 200))
    h = _ERROR_HANDLER
    with h.lock:
        h.flush_pending()
        pending = list(h.pending)
    rows: list[tuple] = []
    path = settings.db_path
    if path and os.path.exists(path):
        try:
            con = sqlite3.connect(path, timeout=0.5)
            try:
                rows = con.execute(
                    "SELECT ts, level, logger, message FROM error_log "
                    "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            rows = []
    # Lo que aun no se ha podido escribir es lo mas nuevo.
    merged = list(reversed(pending)) + list(rows)
    return [{"ts": r[0], "level": r[1], "logger": r[2], "message": r[3]}
            for r in merged[:limit]]


# ---------------- arranque ----------------

def setup() -> None:
    """Configura los logs. Se puede llamar varias veces (una por arranque de
    la app en los tests) sin duplicar handlers ni filtros."""
    level = getattr(logging, settings.log_level, logging.INFO)
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    register_secret(settings.api_key)
    _install_record_factory()

    loggers = [logging.getLogger()] + [logging.getLogger(n) for n in _UVICORN]
    for lg in loggers:
        if _FILTER not in lg.filters:
            lg.addFilter(_FILTER)
        for h in lg.handlers:
            if _FILTER not in h.filters:
                h.addFilter(_FILTER)

    # El registro de errores cuelga del raiz y de «uvicorn», que en la
    # configuracion de uvicorn no propaga al raiz (ahi van sus «Exception in
    # ASGI application»). Un mismo registro no se guarda dos veces.
    for name in ("", "uvicorn"):
        lg = logging.getLogger(name)
        if _ERROR_HANDLER not in lg.handlers:
            lg.addHandler(_ERROR_HANDLER)


def reset_state() -> None:
    """Para los tests: vacia lo pendiente de escribir en error_log (si no, un
    aviso de un test sin BD acabaria en la BD del siguiente)."""
    with _ERROR_HANDLER.lock:
        _ERROR_HANDLER.pending.clear()
