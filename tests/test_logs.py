"""Logs sin secretos y registro de errores recientes (R83, parte de logs).

La clave PRIM, los tokens, las cabeceras Bearer y los codigos de
emparejamiento no pueden aparecer en ningun registro: ni en la consola (los
logs del contenedor), ni en la tabla error_log que ve el panel. Y el log de
acceso no guarda query strings (/api/v1/plan?from=<coordenadas de casa>).
"""
from __future__ import annotations

import io
import logging
import os
import sqlite3

import pytest
from _seg_contrato import ContractClient, auth_limpio, pair, schema_validator  # noqa: F401
from conftest import FAKE_KEY, ruta_j
from fastapi.testclient import TestClient
from fakeprim import Dep

from app import auth, db, logs
from app.config import settings

OTRA = "otra-clave-secreta-123"
HOME = "2.2945;48.8584"


@pytest.fixture
def handler_factory():
    """Handlers de texto enganchados al raiz; se quitan al acabar."""
    creados: list[tuple[logging.Logger, logging.Handler]] = []

    def _make(logger: str = "", formatter: logging.Formatter | None = None):
        buf = io.StringIO()
        h = logging.StreamHandler(buf)
        h.setLevel(logging.DEBUG)
        h.setFormatter(formatter or logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        lg = logging.getLogger(logger)
        lg.addHandler(h)
        creados.append((lg, h))
        return buf

    yield _make
    for lg, h in creados:
        lg.removeHandler(h)


@pytest.fixture
def pendientes_limpios():
    logs.reset_state()
    yield
    logs.reset_state()


def _error_log_dump() -> str:
    with db.conn() as c:
        return repr([dict(r) for r in c.execute("SELECT * FROM error_log")])


def _emitir_secretos(token: str, code: str) -> None:
    log = logging.getLogger("trajet.prueba")
    log.warning("clave en uso %s", FAKE_KEY)
    log.warning("cabeceras: Authorization: Bearer %s", token)
    log.error("token suelto %s y codigo %s", token, code)
    log.warning("peticion a PRIM con apikey=noregistrada123&x=1 y {'apikey': 'otranoreg456'}")
    log.warning('cuerpo {"code": "%s", "device_name": "iPhone"}', code.lower().replace("-", " "))
    log.warning("registrada aparte: %s", OTRA)
    try:
        raise RuntimeError(f"fallo con {FAKE_KEY} y {token} (code={code})")
    except RuntimeError:
        log.exception("algo exploto")
    logging.getLogger("uvicorn.error").error("uvicorn dice Bearer %s", token)
    logging.getLogger().warning("desde el raiz: %s", FAKE_KEY)


def test_secretos_tachados_en_todos_los_registros(app, handler_factory, caplog, pendientes_limpios):
    """R83: la clave, un Bearer, un trj_ y un codigo no aparecen en ningun
    registro ni en error_log."""
    antes = handler_factory()                  # como la consola del contenedor
    with TestClient(app) as tc:                # arranque: logs.setup()
        despues = handler_factory()            # un handler que llega luego
        v1 = ContractClient(tc)
        token = pair(v1)["token"]
        code = auth.new_pairing()["code"]
        logs.register_secret(OTRA)
        _emitir_secretos(token, code)
        entradas = logs.recent_errors(200)
        tabla = _error_log_dump()

    textos = {"antes": antes.getvalue(), "despues": despues.getvalue(),
              "caplog": caplog.text, "error_log": tabla,
              "recent_errors": repr(entradas)}
    prohibidos = [FAKE_KEY, token, token[4:], code, code.replace("-", ""),
                  code.lower().replace("-", " "), OTRA, "noregistrada123", "otranoreg456"]
    for donde, texto in textos.items():
        assert "***" in texto, donde
        for secreto in prohibidos:
            assert secreto not in texto, f"{secreto!r} aparece en {donde}"
    # Se tacha el secreto, no el mensaje entero
    assert "clave en uso ***" in textos["antes"]
    assert "RuntimeError: fallo con *** y trj_***" in textos["antes"]
    assert "Traceback" in textos["antes"]


def test_error_log_forma_y_orden(client, pendientes_limpios):
    log = logging.getLogger("trajet.prueba")
    log.info("esto no va a la tabla")
    log.warning("primero")
    log.error("segundo con %s", FAKE_KEY)
    log.critical("tercero")
    entradas = logs.recent_errors(50)
    assert [e["message"] for e in entradas[:3]] == ["tercero", "segundo con ***", "primero"]
    assert [e["level"] for e in entradas[:3]] == ["CRITICAL", "ERROR", "WARNING"]
    assert all(e["logger"] == "trajet.prueba" for e in entradas[:3])
    assert not any("esto no va" in e["message"] for e in entradas)
    for e in entradas:
        schema_validator("ErrorLogEntry").validate(e)
    assert len(logs.recent_errors(2)) == 2


def test_error_log_conserva_los_ultimos(client, monkeypatch, pendientes_limpios):
    monkeypatch.setattr(logs, "KEEP_ERRORS", 5)
    log = logging.getLogger("trajet.prueba")
    for i in range(8):
        log.warning("aviso %d", i)
    with db.conn() as c:
        filas = [r[0] for r in c.execute("SELECT message FROM error_log ORDER BY id")]
    assert filas == [f"aviso {i}" for i in range(3, 8)]


def test_error_log_sin_bd_se_guarda_en_memoria(env, tmp_path, monkeypatch, pendientes_limpios):
    logs.setup()
    nueva = tmp_path / "todavia" / "trajet.db"
    monkeypatch.setattr(settings, "db_path", str(nueva))
    log = logging.getLogger("trajet.prueba")
    log.error("antes de la BD con %s", FAKE_KEY)          # no revienta
    assert not nueva.exists()                              # y no crea la BD
    assert [e["message"] for e in logs.recent_errors()] == ["antes de la BD con ***"]
    nueva.parent.mkdir()
    db.init()
    log.warning("ya hay BD")
    with db.conn() as c:
        filas = [r[0] for r in c.execute("SELECT message FROM error_log ORDER BY id")]
    assert filas == ["antes de la BD con ***", "ya hay BD"]
    assert not logs._ERROR_HANDLER.pending


def test_error_log_sin_recursion(client, monkeypatch, pendientes_limpios):
    """Si escribir en la BD falla y ademas registra algo, no hay bucle."""
    llamadas = []

    def connect_que_falla(*a, **kw):
        llamadas.append(1)
        logging.getLogger("trajet.dentro").warning("desde dentro del handler")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(logs.sqlite3, "connect", connect_que_falla)
    logging.getLogger("trajet.prueba").error("fuera")
    assert len(llamadas) == 1
    assert [p[3] for p in logs._ERROR_HANDLER.pending] == ["fuera"]


def test_setup_idempotente(client):
    raiz = logging.getLogger()
    antes = (len(raiz.handlers), len(raiz.filters))
    logs.setup()
    logs.setup()
    assert (len(raiz.handlers), len(raiz.filters)) == antes
    assert raiz.handlers.count(logs._ERROR_HANDLER) == 1
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logs._FILTER in logging.getLogger(name).filters


def test_log_de_acceso_sin_query_string(client, capsys):
    """El log de acceso de uvicorn no guarda la query (coordenadas de casa)."""
    from uvicorn.logging import AccessFormatter

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s',
                                   use_colors=False))
    acceso = logging.getLogger("uvicorn.access")
    nivel, propaga = acceso.level, acceso.propagate
    acceso.addHandler(h)
    acceso.setLevel(logging.INFO)
    acceso.propagate = False
    try:
        # Asi lo llama uvicorn (protocols/http/h11_impl.py)
        acceso.info('%s - "%s %s HTTP/%s" %d', "192.168.1.40:51000", "GET",
                    f"/api/v1/plan?from={HOME}&to=stop_area:IDFM:71370&when=08:30", "1.1", 200)
        acceso.info('%s - "%s %s HTTP/%s" %d', "192.168.1.40:51000", "GET",
                    "/api/v1/search/places?q=12+rue+de+ma+casa", "1.1", 200)
    finally:
        acceso.removeHandler(h)
        acceso.setLevel(nivel)
        acceso.propagate = propaga
    out = buf.getvalue()
    assert '"GET /api/v1/plan HTTP/1.1" 200' in out
    assert '"GET /api/v1/search/places HTTP/1.1" 200' in out
    assert "48.8584" not in out and "?" not in out and "casa" not in out
    assert "Logging error" not in capsys.readouterr().err


def test_log_de_httpx_sin_query_string(client, caplog):
    """httpx apunta cada llamada a PRIM con la URL: sin la query."""
    caplog.set_level(logging.INFO, logger="httpx")
    logging.getLogger("httpx").info(
        'HTTP Request: %s %s "%s %d %s"', "GET",
        f"https://prim.iledefrance-mobilites.fr/marketplace/v2/navitia/journeys?from={HOME}&to=x",
        "HTTP/1.1", 200, "OK")
    assert "/marketplace/v2/navitia/journeys " in caplog.text
    assert "48.8584" not in caplog.text


def test_colores_de_uvicorn_tambien_tachados(client, capsys):
    from uvicorn.logging import DefaultFormatter

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(DefaultFormatter("%(levelprefix)s %(message)s", use_colors=True))
    lg = logging.getLogger("uvicorn.error")
    lg.addHandler(h)
    try:
        lg.error("token %s", "trj_" + "C" * 43, extra={"color_message": "token \x1b[1m%s\x1b[0m"})
    finally:
        lg.removeHandler(h)
    assert "C" * 43 not in buf.getvalue() and "trj_***" in buf.getvalue()
    assert "Logging error" not in capsys.readouterr().err


def test_redact_patrones():
    t = "trj_" + "x" * 43
    assert logs.redact(f"Authorization: Bearer {t}") == "Authorization: Bearer ***"
    assert logs.redact("authorization: bearer abc.def-ghi") == "authorization: Bearer ***"
    assert logs.redact(f"el token {t} fin") == "el token trj_*** fin"
    assert logs.redact("PRIM_API_KEY=abcdef123456 resto") == "PRIM_API_KEY=*** resto"
    assert logs.redact("?apikey=zzz&b=1") == "?apikey=***&b=1"
    assert logs.redact("codigo ABCD-EFGH usado") == "codigo *** usado"
    assert logs.redact('{"code": "abcd efgh"}') == '{"code": "***"}'
    # Lo normal no se toca
    for normal in ("linea RER-A", "Gare Saint-Lazare", "HTTP 500 Internal Server Error",
                   "2026-09-24 08:30", "status code: 404", "line_code=J"):
        assert logs.redact(normal) == normal


def test_clave_nunca_en_respuestas_ni_logs(client, fake_prim, caplog, pendientes_limpios):
    """R83: con PRIM fallando de todas las formas, la clave no sale ni en las
    respuestas de la v1 ni en los registros."""
    v1 = ContractClient(client)
    token = pair(v1)["token"]
    cli = v1.with_token(token)
    cli.post("/api/v1/routes", json=ruta_j())
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 5))
    respuestas = [cli.get("/api/v1/board"), cli.get("/api/v1/health")]
    for fallo in (401, 403, 429, 500, "timeout", "connect"):
        fake_prim.fail["*"] = fallo
        from app.api import common
        common._baseline.clear()
        respuestas += [cli.get("/api/v1/board", params={"log_history": "false"}),
                       cli.get("/api/v1/search/stops", params={"q": f"gare {fallo}"}),
                       cli.get("/api/v1/health"), cli.get("/api/v1/ping")]
    todo = "\n".join(r.text + repr(dict(r.headers)) for r in respuestas)
    assert FAKE_KEY not in todo
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in repr(logs.recent_errors(200))
    assert token not in caplog.text
    # (la clave si viaja a PRIM, en su cabecera: es para lo que esta)
    assert FAKE_KEY in fake_prim.seen_keys


def test_bd_de_otro_test_no_se_crea_por_un_log(env, pendientes_limpios):
    """El handler nunca crea trajet.db: eso es cosa de las migraciones."""
    logs.setup()
    assert not os.path.exists(settings.db_path)
    logging.getLogger("trajet.prueba").warning("sin BD todavia")
    assert not os.path.exists(settings.db_path)


def test_filtro_falla_cerrado(monkeypatch):
    """Si tachar falla, el registro sale oculto, no con el secreto."""
    def revienta(_):
        raise RuntimeError("fallo al tachar")

    rec = logging.LogRecord("trajet.prueba", logging.WARNING, __file__, 1,
                            "clave %s", (FAKE_KEY,), None)
    monkeypatch.setattr(logs, "redact", revienta)
    assert logs.RedactingFilter().filter(rec) is True
    assert FAKE_KEY not in rec.getMessage() and "oculto" in rec.getMessage()
