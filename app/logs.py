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
    tachado, para ensenarlo en el panel. Quien registra solo lo deja en
    memoria; lo escribe en SQLite un hilo propio («trajet-error-log»), porque
    muchos avisos salen del bucle de eventos (un fallo de PRIM) y una
    escritura con commit, o esperar a que otro suelte la BD, lo bloqueaba
    entero. Si la BD aun no existe (o esta ocupada) se queda en memoria y se
    escribe en cuanto se pueda.

El filtro se pone en el logger raiz, en los de uvicorn y en sus handlers, y
ademas en la fabrica de registros de logging: asi cubre tambien los loggers
hijos (trajet.prim…) y cualquier handler que se anada despues.
"""
from __future__ import annotations

import atexit
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

# Cuanto texto se tacha como mucho. El mensaje (y la ruta del log de acceso,
# que la elige quien hace la peticion, sin token) se corta a MAX_REDACT; las
# trazas de una excepcion, que escribe Python y pueden ser largas, a
# MAX_REDACT_TRACE y guardando el final, que es donde esta el error. Con eso
# el trabajo de tachar esta acotado por muy larga que venga una URL.
MAX_REDACT = 4096
MAX_REDACT_TRACE = 32768
# Margen al cortar: lo mas largo que puede ser un secreto registrado (una
# clave del panel son hasta 256 caracteres y su forma %-codificada, el
# triple). Ver redact().
_CLIP_GUARD = 1024

# Patrones que se tachan aunque nadie los haya registrado.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Authorization: Bearer <lo que sea>
    (re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer " + MASK),
    # Tokens de dispositivo, aunque vengan sueltos o cortados.
    (re.compile(r"trj_[A-Za-z0-9_-]+"), "trj_" + MASK),
    # apikey=…, PRIM_API_KEY=…, 'apikey': '…', api-key: …
    # Cuantificadores ACOTADOS a proposito (SEC-2): con `\w*` y `\s*`, una
    # ruta hecha de «api_key» repetido obligaba a recorrer el resto del texto
    # desde cada «api» y a volver atras al no encontrar el `:`/`=`: tiempo
    # cuadratico (60 KB de ruta, 5 s con el servidor parado). Con un tope
    # cada intento mira como mucho unas decenas de caracteres.
    (re.compile(r"(api[_-]?key\w{0,32}[\"']?\s{0,8}[:=]\s{0,8}[\"']?)[^\s\"'&,;)}\]]+",
                re.IGNORECASE),
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


def redact(text, limit: int = MAX_REDACT, keep_tail: bool = False) -> str:
    """El texto con los secretos tachados, cortado a `limit` caracteres
    (el principio, o el final con keep_tail) si es mas largo.

    Se corta ANTES de tachar, para que el trabajo no dependa de lo larga que
    llegue una ruta. Pero cortar sin mas podria partir un secreto por la
    mitad y el trozo ya no casaria con nada: se corta con un margen
    (_CLIP_GUARD, mas que el secreto mas largo), se tacha, y se tira el
    margen. Lo unico que puede quedar partido esta pegado al corte, dentro
    de ese margen, y se va con el."""
    if not isinstance(text, str):
        text = str(text)
    clipped = len(text) > limit
    if clipped:
        text = text[-(limit + _CLIP_GUARD):] if keep_tail else text[:limit + _CLIP_GUARD]
    pat = _secrets_re
    if pat is not None:
        text = pat.sub(MASK, text)
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    if clipped:
        text = ("[…] " + text[_CLIP_GUARD:]) if keep_tail else (text[:-_CLIP_GUARD] + " […]")
    return text


def strip_query(path) -> str:
    """/api/v1/plan?from=… -> /api/v1/plan"""
    if not isinstance(path, str):
        return path
    return path.split("?", 1)[0]


# httpx apunta cada peticion a PRIM con la URL entera («HTTP Request: GET
# …/journeys?from=<lon;lat de casa>…»): mismo problema que el log de acceso.
# La query es opcional a proposito: asi el patron casa SIEMPRE desde el
# primer «http» y se come la URL entera de una vez. Con la query obligatoria,
# un texto con muchos «http://» seguidos y sin «?» se recorria entero desde
# cada uno (cuadratico, como SEC-2).
_URL_QUERY = re.compile(r"(\bhttps?://[^\s?#'\"]+)(?:\?[^\s#'\"]*)?")
_QUERY_LOGGERS = ("httpx", "httpcore")


class RedactingFilter(logging.Filter):
    """Tacha los secretos del registro en el sitio. Nunca descarta nada."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_healthcheck(record):
            # El HEALTHCHECK de Docker llama a /api/v1/ping cada 30 s desde
            # dentro del contenedor: 2880 lineas al dia que no dicen nada.
            return False
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


def _is_healthcheck(record: logging.LogRecord) -> bool:
    args = record.args
    if record.name != "uvicorn.access" or not (isinstance(args, tuple) and len(args) == 5):
        return False
    client, _method, path, _version, _status = args
    return (str(client).startswith(("127.0.0.1", "::1"))
            and strip_query(path) == "/api/v1/ping")


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
        record.exc_text = redact(text, MAX_REDACT_TRACE, keep_tail=True)
        # Sin exc_info: un formateador que volviera a pintar la excepcion
        # desde el objeto se saltaria el tachado.
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = redact(record.exc_text, MAX_REDACT_TRACE, keep_tail=True)
    if record.stack_info:
        record.stack_info = redact(record.stack_info, MAX_REDACT_TRACE, keep_tail=True)


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

    emit() corre en el hilo que registra, que muchas veces es el del bucle de
    eventos (un fallo de PRIM, el tablero, el mapa): ahi solo se deja el
    registro en memoria (`pending`) y se avisa al hilo «trajet-error-log»,
    que es el que abre SQLite, escribe y hace commit. Antes se escribia en
    emit() y cada aviso paraba el bucle; con otra escritura en curso, hasta
    medio segundo (H4 de la verificacion).

    No usa db.conn() a proposito: un log no puede crear la BD antes de las
    migraciones ni esperar 10 s a un bloqueo. Si la BD o la tabla aun no
    existen (o esta ocupada), lo pendiente se queda en memoria y se reintenta
    con el siguiente aviso o a los RETRY segundos. Nunca registra nada desde
    dentro (sin recursion).

    Cerrojos, siempre en este orden: `_io` (una escritura a la vez: el hilo,
    recent_errors o flush) y luego `self.lock` (el del Handler, que ya tiene
    quien registra durante emit; aqui solo se toma un instante para copiar o
    quitar de `pending`, nunca mientras se escribe)."""

    RETRY = 5.0

    def __init__(self):
        super().__init__(level=logging.WARNING)
        # (ts, level, logger, message, n): n crece con cada registro y dice
        # que se ha escrito ya aunque el deque haya tirado los mas viejos.
        self.pending: deque = deque(maxlen=KEEP_ERRORS)
        self._n = 0
        self._local = threading.local()
        self._io = threading.Lock()
        self._wake = threading.Event()
        self._writer: threading.Thread | None = None
        self._writer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "busy", False) or getattr(record, "_trajet_stored", False):
            return
        try:
            record._trajet_stored = True
            _FILTER.filter(record)
            message = redact(record.getMessage())
            if record.exc_text:
                message = f"{message}\n{redact(record.exc_text, MAX_REDACT_TRACE, keep_tail=True)}"
            if len(message) > MAX_MESSAGE:
                message = message[:MAX_MESSAGE] + " […]"
            ts = datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds")
            # Aqui ya se tiene self.lock (Handler.handle): n y pending van juntos.
            self._n += 1
            self.pending.append((ts, _level_name(record.levelno), record.name, message, self._n))
            self._start_writer()
            self._wake.set()
        except Exception:  # noqa: S110
            # Ni propagar ni registrar (seria recursivo) ni handleError (lo
            # pintaria en stderr en cada aviso).
            pass

    # ---------------- el hilo que escribe ----------------

    def _start_writer(self) -> None:
        t = self._writer
        if t is not None and t.is_alive():
            return
        with self._writer_lock:
            if self._writer is None or not self._writer.is_alive():
                self._writer = threading.Thread(target=self._run, name="trajet-error-log",
                                                daemon=True)
                self._writer.start()

    def _run(self) -> None:
        while True:
            # Con algo sin escribir (BD ocupada o aun sin crear) se reintenta
            # cada RETRY s; si no, a dormir hasta el siguiente aviso.
            self._wake.wait(self.RETRY if self.pending else None)
            self._wake.clear()
            try:
                self.flush_pending()
            except Exception:  # noqa: S110
                pass    # un fallo raro no mata el hilo: se reintenta con el siguiente aviso

    def flush_pending(self) -> bool:
        """Escribe lo pendiente en error_log. True si no queda nada.

        Abre SQLite: la llaman el hilo del registro, recent_errors (desde un
        hilo del threadpool) y flush(); nunca el bucle de eventos."""
        with self._io:
            return self._write_pending()

    def _write_pending(self) -> bool:
        """flush_pending con `_io` ya tomado."""
        with self.lock:
            items = list(self.pending)
        if not items:
            return True
        path = settings.db_path
        if not path or not os.path.exists(path):
            return False
        # Lo que se registre mientras se escribe (p. ej. desde sqlite3) se
        # descarta: si no, un fallo al escribir que avisara de si mismo seria
        # un bucle sin fin.
        self._local.busy = True
        try:
            try:
                con = sqlite3.connect(path, timeout=0.5)
            except sqlite3.Error:
                return False
            try:
                con.executemany("INSERT INTO error_log (ts, level, logger, message) "
                                "VALUES (?,?,?,?)", [item[:4] for item in items])
                con.execute("DELETE FROM error_log WHERE id <= "
                            "(SELECT MAX(id) FROM error_log) - ?", (KEEP_ERRORS,))
                con.commit()
            except sqlite3.Error:
                return False
            finally:
                con.close()
        finally:
            self._local.busy = False
        last = items[-1][4]
        with self.lock:
            while self.pending and self.pending[0][4] <= last:
                self.pending.popleft()
            return not self.pending

    def clear_pending(self) -> None:
        with self._io, self.lock:
            self.pending.clear()


_ERROR_HANDLER = ErrorLogHandler()


def flush() -> bool:
    """Escribe ya en error_log lo pendiente, en el hilo que llama. Para los
    tests (que leen la tabla a mano) y el apagado; nunca desde el bucle de
    eventos. True si no queda nada."""
    return _ERROR_HANDLER.flush_pending()


# Al salir, lo que el hilo no haya llegado a escribir (es daemon: se para sin
# avisar). Se registra despues que logging, asi que corre antes que su
# logging.shutdown.
atexit.register(flush)


def recent_errors(limit: int = 50) -> list[dict]:
    """ErrorLogEntry del contrato, del mas nuevo al mas viejo (1..200).

    Lee SQLite: desde una ruta, con run_in_threadpool."""
    limit = max(1, min(int(limit), 200))
    h = _ERROR_HANDLER
    rows: list[tuple] = []
    # Con `_io` tomado de principio a fin: el hilo no puede escribir entre la
    # foto de lo pendiente y la lectura de la tabla (saldria repetido).
    with h._io:
        h._write_pending()
        with h.lock:
            pending = [item[:4] for item in h.pending]
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
    aviso de un test sin BD acabaria en la BD del siguiente). Espera a que
    acabe una escritura en curso del hilo."""
    _ERROR_HANDLER.clear_pending()
