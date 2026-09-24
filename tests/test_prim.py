"""Cliente de PRIM: cabecera y base (R63), copia con su edad (R65), una
llamada por clave de cache (R66), errores tipados, pausa tras fallos, cache
acotada, last_error sin parametros, clave en caliente y comprobacion de
claves; y la clave nunca sale del servidor (R83, en parte)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import pytest
import yaml
from conftest import FAKE_KEY, ruta_j
from fakeprim import Dep, Msg
from jsonschema import Draft202012Validator

from app.prim import PrimError

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "contract")
FIXTURES = os.path.join(HERE, "fixtures", "prim")

NUEVA = "NUEVA0clave0de0mentira0000wxyz"
SL = "STIF:StopArea:SP:71370:"
ARG = "STIF:StopArea:SP:65063:"


def _validator(name: str) -> Draft202012Validator:
    with open(os.path.join(DOCS, "openapi.yaml"), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}",
                                 "components": spec["components"]})


@pytest.fixture
async def pc(env, fake_prim):
    """El cliente de PRIM arrancado como en el servidor, contra el PRIM falso."""
    from app import db, prim
    db.init()
    await prim.startup()
    yield prim.get_client()
    await prim.shutdown()


def _trajet_logs(caplog) -> str:
    """Solo los logs de la app (los de httpx los limpia logs.py)."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("trajet"))


# ---------------- R63 ----------------

async def test_prim_cabecera_apikey_y_base(pc, fake_prim):
    await pc.stop_monitoring(SL)
    await pc.general_message()
    await pc.places("argenteuil")
    assert fake_prim.paths == ["/marketplace/stop-monitoring", "/marketplace/general-message",
                               "/marketplace/v2/navitia/places"]
    assert fake_prim.seen_keys == [FAKE_KEY] * 3


# ---------------- errores tipados ----------------

@pytest.mark.parametrize("fallo, kind, msg", [
    (401, "invalid", "clave no válida (HTTP 401)"),
    (403, "forbidden", "sin permiso para esta API (HTTP 403)"),
    (429, "quota", "cuota agotada (HTTP 429)"),
    (500, "http", "HTTP 500"),
    (503, "http", "HTTP 503"),
    ("timeout", "unreachable", "tiempo de espera agotado"),
    ("connect", "unreachable", "no se puede conectar"),
])
async def test_prim_errores_tipados(pc, fake_prim, fallo, kind, msg):
    fake_prim.fail["stop-monitoring"] = fallo
    if fallo == 429:
        fake_prim.remaining["stop-monitoring"] = 1          # 429 de la cuota del dia
    with pytest.raises(PrimError) as e:
        await pc.stop_monitoring(SL)
    assert (e.value.kind, str(e.value), e.value.endpoint) == (kind, msg, "stop-monitoring")
    assert pc.last_error == f"stop-monitoring: {msg}"
    # Solo cuenta lo que llego a PRIM: un fallo de conexion no.
    assert pc.quota_counter.used("stop-monitoring") == (0 if fallo == "connect" else 1)
    if fallo == 401:
        assert pc.store.info()["state"] == "invalid"
    if fallo == 403:
        assert pc.store.info()["state"] == "forbidden"
    if fallo == 429:
        assert pc.quota_counter.can_spend("stop-monitoring") is False
        assert pc.quota_counter.level("stop-monitoring") == "exhausted"

    # La siguiente no vuelve a llamar (pausa o cuota agotada) y falla igual.
    antes = fake_prim.calls["stop-monitoring"]
    with pytest.raises(PrimError) as e2:
        await pc.stop_monitoring(ARG)
    assert fake_prim.calls["stop-monitoring"] == antes and e2.value.kind == kind


async def test_429_de_rafaga_no_agota_el_dia(pc, fake_prim):
    """Un 429 con cuota en la cabecera es un limite por segundo: pausa corta,
    no se da el dia por perdido."""
    fake_prim.fail["stop-monitoring"] = 429          # la cabecera aun dice 899
    with pytest.raises(PrimError) as e:
        await pc.stop_monitoring(SL)
    assert e.value.kind == "quota"
    assert pc.quota_counter.can_spend("stop-monitoring") is True
    assert pc.paused("stop-monitoring")


async def test_pausa_creciente_hasta_60s(pc, fake_prim):
    reloj = [1000.0]
    pc._clock = lambda: reloj[0]
    fake_prim.fail["stop-monitoring"] = 500
    esperas = []
    for _ in range(7):
        with pytest.raises(PrimError):
            await pc.stop_monitoring(SL)
        esperas.append(pc._pauses["stop-monitoring"].until - reloj[0])
        hechas = fake_prim.calls["stop-monitoring"]
        with pytest.raises(PrimError):                     # en pausa: ni se llama
            await pc.stop_monitoring(ARG)
        assert fake_prim.calls["stop-monitoring"] == hechas
        assert pc.degraded() is True
        reloj[0] += esperas[-1] + 0.1
    assert esperas == [5, 10, 20, 40, 60, 60, 60]
    assert fake_prim.calls["stop-monitoring"] == 7

    fake_prim.fail.clear()
    data, age = await pc.stop_monitoring(SL)
    assert age == 0.0 and "stop-monitoring" not in pc._pauses
    assert pc.last_error is None


async def test_un_404_no_pausa_el_endpoint(pc, fake_prim):
    """Un 404 es de esa peticion (p. ej. una parada que no existe)."""
    fake_prim.fail["navitia"] = 404
    with pytest.raises(PrimError) as e:
        await pc.lines_at_stop("stop_area:IDFM:0")
    assert e.value.kind == "http" and not pc.paused("navitia")
    fake_prim.fail.clear()
    await pc.lines_at_stop("stop_area:IDFM:71370")
    assert fake_prim.calls["navitia"] == 2


# ---------------- R65, R66 ----------------

async def test_prim_fallo_sirve_copia_con_edad(pc, fake_prim):
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6))
    d1, age1 = await pc.stop_monitoring(SL)
    assert age1 == 0.0
    pc._cache[f"sm:{SL}"].fetched_at -= 90
    fake_prim.fail["*"] = "timeout"
    d2, age2 = await pc.stop_monitoring(SL)
    assert d2 == d1 and 89 <= age2 <= 92
    assert pc.last_error == "stop-monitoring: tiempo de espera agotado"
    assert pc.degraded() is True
    # En pausa: la copia sale sin volver a llamar.
    d3, _ = await pc.stop_monitoring(SL)
    assert d3 == d1 and fake_prim.calls["stop-monitoring"] == 2
    # Sin copia y con PRIM caido: error.
    with pytest.raises(PrimError):
        await pc.stop_monitoring(ARG)


async def test_prim_lock_una_llamada(pc, fake_prim):
    fake_prim.delay = 0.05
    res = await asyncio.gather(*[pc.stop_monitoring(SL) for _ in range(5)])
    assert len(res) == 5 and fake_prim.calls["stop-monitoring"] == 1
    # Tres estaciones distintas a la vez: tres llamadas, y ningun lock se queda.
    await asyncio.gather(*[pc.stop_monitoring(f"STIF:StopArea:SP:{n}:")
                           for n in ("1", "2", "3")])
    assert fake_prim.calls["stop-monitoring"] == 4
    assert pc._locks == {}


async def test_cache_acotada_lru(pc, fake_prim):
    pc.cache_max = 3
    for q in ("uno", "dos", "tres", "cuatro"):
        await pc.places(q)
    assert list(pc._cache) == ["pl:stop_area:dos", "pl:stop_area:tres",
                               "pl:stop_area:cuatro"]
    await pc.places("dos")                    # de la cache: pasa a ser la reciente
    assert fake_prim.calls["navitia"] == 4
    await pc.places("cinco")
    assert list(pc._cache) == ["pl:stop_area:cuatro", "pl:stop_area:dos",
                               "pl:stop_area:cinco"]
    assert len(pc._cache) <= 3


# ---------------- last_error y logs sin parametros ----------------

async def test_last_error_sin_parametros(pc, fake_prim, caplog):
    caplog.set_level(logging.DEBUG)
    fake_prim.fail["navitia"] = 500
    with pytest.raises(PrimError) as e:
        await pc.places("6 rue de la marseillaise")
    assert str(e.value) == "HTTP 500" and pc.last_error == "navitia: HTTP 500"
    pc._pauses.clear()
    with pytest.raises(PrimError):
        await pc.journeys("2.2170;48.9270", "2.4040;48.8930")
    textos = _trajet_logs(caplog) + (pc.last_error or "") + str(e.value)
    for dato in ("marseillaise", "48.927", "2.404", "prim.iledefrance", "?", "pl:", "jr:"):
        assert dato not in textos, dato


async def test_last_error_por_endpoint(pc, fake_prim):
    """Una llamada buena a OTRO endpoint no borra el fallo que sigue ahi."""
    fake_prim.fail["general-message"] = 503
    with pytest.raises(PrimError):
        await pc.general_message()
    await pc.stop_monitoring(SL)
    assert pc.last_error == "general-message: HTTP 503"
    fake_prim.fail.clear()
    pc._pauses.clear()
    await pc.general_message()
    assert pc.last_error is None


# ---------------- sin clave ----------------

async def test_sin_clave(env, fake_prim, monkeypatch):
    from app import db, prim
    from app.config import settings
    monkeypatch.setenv("PRIM_API_KEY", "")
    settings.reload()
    db.init()
    await prim.startup()
    try:
        with pytest.raises(PrimError) as e:
            await prim.get_client().stop_monitoring(SL)
        assert e.value.kind == "no_key" and fake_prim.total_calls() == 0
        assert prim.server_state()["prim_key"] == "missing"
        st = prim.prim_state()
        assert st["key_state"] == "missing" and st["key_source"] == "none"
        _validator("PrimState").validate(st)
        _validator("ServerState").validate(prim.server_state())
        _validator("QuotaV1").validate(prim.quota_snapshot())
    finally:
        await prim.shutdown()


# ---------------- comprobar claves ----------------

@pytest.mark.parametrize("fallo, status, texto", [
    (None, 200, "responde bien"),
    (401, 401, "no válida"),
    (403, 403, "permiso"),
    (429, 429, "cuota diaria agotada"),
    (500, 500, "PRIM caído"),
    ("timeout", None, "no responde"),
])
async def test_validate_key_con_el_prim_falso(pc, fake_prim, fallo, status, texto):
    if fallo is not None:
        fake_prim.fail["*"] = fallo
    checks = await pc.validate_key(NUEVA)
    v = _validator("PrimKeyCheck")
    for c in checks:
        v.validate(c)
    assert [c["api"] for c in checks] == ["stop-monitoring", "general-message", "navitia"]
    assert all(c["ok"] is (fallo is None) and c["status"] == status for c in checks)
    assert all(texto in c["message"] for c in checks)
    # Una llamada minima por API, con la clave nueva...
    assert sorted(fake_prim.paths) == ["/marketplace/general-message",
                                       "/marketplace/stop-monitoring",
                                       "/marketplace/v2/navitia/places"]
    assert set(fake_prim.seen_keys) == {NUEVA}
    # ...sin tocar la clave en uso, ni su cuota, ni sus pausas.
    assert pc._key == FAKE_KEY and pc._pauses == {}
    assert all(pc.quota_counter.used(ep) == 0 for ep in ("stop-monitoring",
                                                          "general-message", "navitia"))


async def test_guardar_clave_rechazada_no_guarda_nada(pc, fake_prim):
    from app import prim
    fake_prim.fail["navitia"] = 403
    res = await prim.set_key_and_save(NUEVA)
    _validator("PrimKeyResult").validate(res)
    assert res["saved"] is False and res["error"]["code"] == "prim_key_rejected"
    assert "navitia" in res["error"]["message"]
    assert res["info"]["source"] == "env" and pc._key == FAKE_KEY
    assert not os.path.exists(pc.store.path)

    fake_prim.fail = {"*": "timeout"}
    res = await prim.set_key_and_save(NUEVA)
    assert res["saved"] is False and res["error"]["code"] == "prim_unreachable"
    res = await prim.set_key_and_save("   ")
    assert res["saved"] is False and res["error"]["code"] == "bad_request"


async def test_guardar_borrar_y_recomprobar(pc, fake_prim):
    from app import prim
    res = await prim.set_key_and_save(NUEVA)
    _validator("PrimKeyResult").validate(res)
    assert res["saved"] is True and "error" not in res
    assert res["info"]["source"] == "panel" and res["info"]["state"] == "valid"
    assert res["info"]["last4"] == NUEVA[-4:] and pc._key == NUEVA
    with open(pc.store.path, "rb") as f:
        assert NUEVA.encode() not in f.read()

    ok = await prim.recheck()
    _validator("PrimKeyResult").validate(ok)
    assert ok["saved"] is False and "error" not in ok and ok["info"]["state"] == "valid"

    fake_prim.fail["navitia"] = 403
    mal = await prim.recheck()
    assert mal["error"]["code"] == "prim_key_rejected"
    assert mal["info"]["state"] == "forbidden" and pc.key_state() == "forbidden"

    info = await prim.delete_saved_key()
    _validator("PrimKeyInfo").validate(info)
    assert info["source"] == "env" and pc._key == FAKE_KEY
    fake_prim.seen_keys.clear()
    await pc.stop_monitoring(SL)
    assert fake_prim.seen_keys == [FAKE_KEY]


async def test_borrar_sin_clave_de_entorno_se_queda_sin_clave(env, fake_prim, monkeypatch):
    from app import db, prim
    from app.config import settings
    monkeypatch.setenv("PRIM_API_KEY", "")
    settings.reload()
    db.init()
    await prim.startup()
    try:
        assert (await prim.set_key_and_save(NUEVA))["saved"] is True
        await prim.get_client().stop_monitoring(SL)
        info = await prim.delete_saved_key()
        assert info["configured"] is False and info["state"] == "missing"
        with pytest.raises(PrimError) as e:
            await prim.get_client().stop_monitoring(ARG)
        assert e.value.kind == "no_key"
        assert (await prim.recheck())["error"]["code"] == "prim_key_missing"
    finally:
        await prim.shutdown()


async def test_set_key_lee_la_cuota_en_un_hilo(pc, fake_prim, monkeypatch):
    """H4: al cambiar la clave desde el panel, lo que la clave lleva gastado
    hoy se lee de SQLite fuera del bucle de eventos. Y se lee bien: volver a
    una clave recupera su cuenta."""
    import threading

    from app import db
    await pc.stop_monitoring(SL)
    assert pc.quota_counter.used("stop-monitoring") == 1
    bucle = threading.get_ident()
    hilos: list[int] = []
    real_conn = db.conn

    def conn_apuntada():
        hilos.append(threading.get_ident())
        return real_conn()

    monkeypatch.setattr(db, "conn", conn_apuntada)
    await pc.set_key(NUEVA)
    assert pc.quota_counter.used("stop-monitoring") == 0       # clave nueva: de cero
    await pc.set_key(FAKE_KEY)
    assert pc.quota_counter.used("stop-monitoring") == 1       # la de antes: su cuenta
    assert hilos and bucle not in hilos


async def test_degraded_solo_mira_el_tablero(pc, fake_prim):
    """H10: ServerState.degraded va en cada tablero; que falle navitia (el
    planificador o el buscador) no hace viejo el tablero. Una copia vieja o
    una pausa de stop-monitoring o general-message, si."""
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6))
    await pc.stop_monitoring(SL)
    await pc.general_message()
    await pc.places("gare")
    assert pc.degraded() is False
    fake_prim.fail["navitia"] = "timeout"
    with pytest.raises(PrimError):
        await pc.places("argenteuil")                           # sin copia: error
    pc._cache["pl:stop_area:gare"].fetched_at -= 10 ** 6
    await pc.places("gare")                                     # copia vieja de navitia
    assert pc.paused("navitia") and pc._stale_at.get("navitia")
    assert pc.degraded() is False
    # El tablero con una copia vieja: eso si es degradado.
    pc._cache[f"sm:{SL}"].fetched_at -= 90
    fake_prim.fail["stop-monitoring"] = 500
    _, age = await pc.stop_monitoring(SL)
    assert age >= 89 and pc.degraded() is True
    # Y pasada la ventana (y sin pausa), deja de serlo.
    reloj = pc._clock
    pc._clock = lambda: reloj() + 3600
    assert pc.degraded() is False


# ---------------- con el servidor arrancado ----------------

def test_set_key_en_caliente_con_el_servidor_arrancado(client, fake_prim):
    """Guardar una clave nueva la pone en uso al momento: cache vacia,
    contador nuevo y la siguiente peticion ya va con ella."""
    from app import prim
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6))
    client.post("/api/routes", json=ruta_j())
    assert client.get("/api/board").json()["legs"][0]["departures"]
    assert set(fake_prim.seen_keys) == {FAKE_KEY}
    assert prim.quota_snapshot()["endpoints"][0]["used"] == 1

    res = client.portal.call(prim.set_key_and_save, NUEVA)
    assert res["saved"] is True
    assert prim.quota_snapshot()["endpoints"][0]["used"] == 0      # contador nuevo

    n = len(fake_prim.seen_keys)
    body = client.get("/api/board").json()                 # sin esperar al TTL
    assert body["legs"][0]["departures"]
    assert fake_prim.seen_keys[n:] and set(fake_prim.seen_keys[n:]) == {NUEVA}
    assert prim.quota_snapshot()["endpoints"][0]["used"] == 1
    assert prim.prim_state()["key_source"] == "panel"
    assert client.get("/api/health").json()["key_configured"] is True


def test_clave_nunca_expuesta(client, fake_prim, caplog):
    """R83: ni la clave del entorno ni la del panel salen en respuestas,
    logs ni errores; como mucho sus 4 ultimos caracteres."""
    from app import prim
    caplog.set_level(logging.DEBUG)
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6))
    fake_prim.message(Msg(["C01739"], "Trafic perturbé."))
    client.post("/api/routes", json=ruta_j())
    client.get("/api/board")
    assert client.portal.call(prim.set_key_and_save, NUEVA)["saved"] is True
    fake_prim.fail["navitia"] = 401                           # y con errores
    cuerpos = []
    for path in ("/api/health", "/api/v1/health", "/api/v1/board", "/api/board",
                 "/api/search/stops?q=argenteuil", "/api/admin/prim-key",
                 "/api/admin/overview", "/api/admin/quota"):
        cuerpos.append(client.get(path).text)
    cuerpos.append(json.dumps(client.portal.call(prim.recheck)))
    cuerpos.append(json.dumps([prim.prim_state(), prim.server_state(),
                               prim.get_keystore().info()]))
    todo = "\n".join(cuerpos) + caplog.text
    for clave in (NUEVA, FAKE_KEY):
        assert clave not in todo
        assert clave[:-4] not in todo


# ---------------- una respuesta real de PRIM, sin red ----------------

_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


def _al_presente(node, delta):
    """Mueve todas las horas SIRI de una respuesta guardada a hoy, para que
    el tablero no las descarte por pasadas."""
    if isinstance(node, dict):
        return {k: _al_presente(v, delta) for k, v in node.items()}
    if isinstance(node, list):
        return [_al_presente(v, delta) for v in node]
    if isinstance(node, str) and _TS.match(node):
        dt = datetime.fromisoformat(node.replace("Z", "+00:00")) + delta
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return node


def _fixture(name: str) -> dict:
    path = os.path.join(FIXTURES, name)
    if not os.path.exists(path):
        pytest.skip(f"falta tests/fixtures/prim/{name} (se genera con scripts/test-real.sh)")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_respuesta_real_de_prim_pasa_por_el_tablero():
    """Saint-Lazare y general-message tal cual los dio PRIM (tests/real)."""
    from app import board
    from app.idfm import line_code, real_platform
    sm = _fixture("stop-monitoring-saint-lazare.json")
    visitas = sm["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"][0]["MonitoredStopVisit"]
    assert 0 < len(visitas) <= 40
    enviado = datetime.fromisoformat(
        sm["Siri"]["ServiceDelivery"]["ResponseTimestamp"].replace("Z", "+00:00"))
    sm = _al_presente(sm, datetime.now(timezone.utc) - enviado)

    lineas = {line_code(v["MonitoredVehicleJourney"]["LineRef"]["value"]) for v in visitas}
    vistas = 0
    for code in lineas:
        leg = {"line_id": f"line:IDFM:{code}", "directions": []}
        deps = board.extract_departures(sm, leg, limit=60)
        vistas += len(deps)
        for d in deps:
            assert d["minutes"] >= 0 and re.match(r"^\d{2}:\d{2}$", d["at"])
            assert d["destination"]
            # R73: 'unknown' o el nombre de la estacion nunca son una via.
            assert d["platform"] is None or d["platform"] == real_platform(d["platform"])
            assert d["length"] in (None, "short", "long")
    assert vistas > 0

    gm = _fixture("general-message.json")
    enviado = datetime.fromisoformat(
        gm["Siri"]["ServiceDelivery"]["ResponseTimestamp"].replace("Z", "+00:00"))
    avisos = board.index_disruptions(_al_presente(gm, datetime.now(timezone.utc) - enviado))
    assert avisos
    for code, entradas in avisos.items():
        assert re.match(r"^C\d{5}$", code)
        assert all(e["severity"] in (board.DISRUPTED, board.INTERRUPTED) for e in entradas)
        st = board.line_status(code, avisos)
        assert st["label"] in ("normal", "perturbada", "interrumpida")
