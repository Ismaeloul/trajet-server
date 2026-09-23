"""Los casos de PreviewData pasados por el tablero de verdad (/api/board).

Cada test monta un escenario de tests/scenarios.py en el PRIM falso, guarda
la ruta y comprueba contra la respuesta real las reglas de docs/reglas.md
que ese caso cubre (R2-R6, R8, R10, R24-R26, R65-R67, R71, R74).
"""
from __future__ import annotations

import asyncio
import os
import time

import pytest
import scenarios as S
import yaml
from jsonschema import Draft202012Validator

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contract")


def _board_030() -> Draft202012Validator:
    with open(os.path.join(DOCS, "openapi-0.3.0.yaml"), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    return Draft202012Validator({"$ref": "#/components/schemas/Board",
                                 "components": spec["components"]})


def _montar(client, caso: S.Caso) -> int:
    r = client.post("/api/routes", json=caso.ruta)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _tablero(client) -> dict:
    r = client.get("/api/board")
    assert r.status_code == 200, r.text
    return r.json()


def _deps(body: dict, seq: int = 0) -> list[dict]:
    return body["legs"][seq]["departures"]


def _envejecer(segundos: float) -> None:
    """Hace que todo lo que hay en la cache de PRIM tenga `segundos` mas."""
    from app import prim
    for entry in prim.get_client()._cache.values():
        entry.fetched_at -= segundos


# ---------------- via: R2, R10, R73, R74 ----------------

def test_R2_sin_via_no_hay_hueco(client, fake_prim):
    """Saint-Lazare manda 'unknown': no hay via (null), ni probable sin datos."""
    _montar(client, S.sin_via(fake_prim))
    deps = _deps(_tablero(client))
    assert len(deps) == 2
    assert all(d["platform"] is None and d["platform_new"] is False for d in deps)
    assert all("guess" not in d for d in deps)


def test_R2_R74_via_que_aparece(client, fake_prim):
    """Primero sin via; cuando PRIM la publica, platform_new una sola vez."""
    caso = S.via_que_aparece(fake_prim)
    _montar(client, caso)
    antes = _deps(_tablero(client))[0]
    assert antes["platform"] is None and antes["platform_new"] is False

    caso.paso2()
    ahora = _deps(_tablero(client))[0]
    assert ahora["platform"] == "21" and ahora["platform_new"] is True
    assert ahora["train"] == "135711"

    caso.paso2()                          # otro refresco con la misma via
    despues = _deps(_tablero(client))[0]
    assert despues["platform"] == "21" and despues["platform_new"] is False
    assert fake_prim.calls["stop-monitoring"] == 3


def test_R10_via_probable_frente_a_real(client, fake_prim):
    """Via real sin prevision; previsiones por mision (82 %) y por hora."""
    _montar(client, S.via_probable(fake_prim))
    e1, e2, e3 = _deps(_tablero(client))
    assert e1["platform"] == "11" and "guess" not in e1
    assert e2["platform"] is None
    assert e2["guess"]["platform"] == "11" and e2["guess"]["basis"] == "mision"
    assert e2["guess"]["share"] == 0.82 and e2["guess"]["samples"] == 17
    assert e3["platform"] is None
    assert e3["guess"]["platform"] == "7" and e3["guess"]["basis"] == "hora"
    assert e3["guess"]["share"] == 0.56


# ---------------- modos, horas y formato: R3, R4, R5, R6, R71 ----------------

def test_R3_modos_sin_via_no_reservan_hueco(client, fake_prim):
    """Metro, bus y tranvia: platform_expected false; tren y RER: true."""
    from app.api import common
    _montar(client, S.casa_trabajo(fake_prim))
    data = client.portal.call(common.board, None, False)
    esperado = {"Bus": False, "Tramway": False, "RER": True, "Métro": False}
    for leg in data["legs"]:
        assert leg["platform_expected"] is esperado[leg["line_mode"]], leg["line_mode"]
        if not leg["platform_expected"]:
            assert all(d["platform"] is None for d in leg["departures"])


def test_R4_sin_hora_teorica_no_hay_retraso(client, fake_prim):
    _montar(client, S.casa_trabajo(fake_prim))
    body = _tablero(client)
    bus, _, rer, metro, bus147 = body["legs"]
    assert [d["delay"] for d in bus["departures"]] == [11, 1, 1]
    assert bus["departures"][0]["status"] == "delayed"
    assert all(d["delay"] is None for d in metro["departures"] + bus147["departures"])
    assert all(d["aimed_at"] == "" for d in metro["departures"])
    assert [d["delay"] for d in rer["departures"]] == [2, 0, None]


def test_R5_longitud_del_tren_y_sin_ocupacion(client, fake_prim):
    _montar(client, S.casa_trabajo(fake_prim))
    body = _tablero(client)
    assert [d["length"] for d in body["legs"][2]["departures"]] == ["long", "short", None]
    for leg in body["legs"]:
        for d in leg["departures"]:
            assert not any("occup" in k.lower() for k in d)


@pytest.mark.parametrize("nombre, minutos", [("bus_106", [6, 56, 106]),
                                             ("bus_165", [60, 165])])
def test_R6_minutos_largos_llegan_enteros(client, fake_prim, nombre, minutos):
    """El servidor manda los minutos tal cual (la app pinta 1h46, 2h45)."""
    _montar(client, S.ESCENARIOS[nombre](fake_prim))
    assert [d["minutes"] for d in _deps(_tablero(client))] == minutos


def test_R71_salida_pasada_fuera_y_minutos_nunca_negativos(client, fake_prim):
    _montar(client, S.salidas_pasadas(fake_prim))
    deps = _deps(_tablero(client))
    assert [d["minutes"] for d in deps] == [0, 4]


def test_tren_parado_en_el_anden(client, fake_prim):
    _montar(client, S.tren_en_anden(fake_prim))
    primero = _deps(_tablero(client))[0]
    assert primero["at_stop"] is True and primero["minutes"] == 0


# ---------------- avisos: R8, R25 ----------------

async def _cancelar_traducciones():
    from app import translate
    tareas = list(translate._en_curso.values())
    for t in tareas:
        t.cancel()
    await asyncio.gather(*tareas, return_exceptions=True)


def test_R8_aviso_en_frances_sin_esperar_a_la_traduccion(client, fake_prim, monkeypatch):
    """El frances sale ya con «traduciendo»; la traduccion llega en el
    refresco siguiente y el original no se pierde."""
    from app import translate
    from app.config import settings

    async def modelo_lento(text):
        await asyncio.sleep(3600)

    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid:11434")
    monkeypatch.setattr(translate, "translate", modelo_lento)
    _montar(client, S.aviso_frances(fake_prim))

    t0 = time.monotonic()
    st = _tablero(client)["legs"][0]["status"]
    assert time.monotonic() - t0 < 3            # nunca se espera al modelo
    assert st["label"] == "perturbada" and st["messages"] == [S.AVISO_147]
    assert st["messages_es"] == [None] and st["translating"] is True
    client.portal.call(_cancelar_traducciones)

    translate.store(S.AVISO_147, S.AVISO_147_ES)
    st = _tablero(client)["legs"][0]["status"]
    assert st["messages"] == [S.AVISO_147]      # el frances sigue ahi
    assert st["messages_es"] == [S.AVISO_147_ES] and st["translating"] is False


def test_R25_tramo_vacio_cortado_frente_a_finalizado(client, fake_prim):
    """Sin salidas: nivel 2 si la linea esta cortada, normal si no."""
    _montar(client, S.linea_cortada(fake_prim))
    leg = _tablero(client)["legs"][0]
    assert leg["departures"] == []
    assert leg["status"]["level"] == 2 and leg["status"]["label"] == "interrumpida"
    assert leg["status"]["messages"] == [S.AVISO_13_CORTADA]


def test_R25_tramo_vacio_sin_avisos_es_servicio_finalizado(client, fake_prim):
    _montar(client, S.tramo_vacio(fake_prim))
    leg = _tablero(client)["legs"][0]
    assert leg["departures"] == []
    assert leg["status"]["level"] == 0 and leg["status"]["label"] == "normal"


def test_R24_destinos_mezclados(client, fake_prim):
    """Tramo sin sentido: cada salida lleva su destino y no se cuela la J."""
    _montar(client, S.destinos_mezclados(fake_prim))
    deps = _deps(_tablero(client))
    assert [d["destination"] for d in deps] == [
        "Saint-Denis Université", "Asnières-Gennevilliers Les Courtilles",
        "Saint-Denis Université"]


# ---------------- el caso dificil: 5 tramos ----------------

def test_casa_trabajo_cinco_tramos(client, fake_prim):
    caso = S.casa_trabajo(fake_prim)
    _montar(client, caso)
    body = _tablero(client)
    _board_030().validate(body)
    assert body["route"]["name"] == "Casa → Trabajo" and body["auto_selected"] is True
    assert body["errors"] == [] and body["stale"] is False
    assert body["worst_level"] == 2 and body["worst_line"] == "13"
    assert body["max_delay"] == 11
    bus, t2, rer, metro, bus147 = body["legs"]
    assert [d["minutes"] for d in bus["departures"]] == [6, 56, 106]
    assert t2["departures"] == [] and t2["status"]["label"] == "normal"
    assert rer["status"]["level"] == 0 and rer["status"]["planned"] == 1
    assert metro["status"]["label"] == "interrumpida"
    assert metro["departures"][0]["at_stop"] is True
    assert len({d["destination"] for d in metro["departures"]}) == 2
    assert bus147["status"]["messages_es"] == [S.AVISO_147_ES]
    # R64: una llamada por estacion (5 estaciones) y una de avisos.
    assert fake_prim.calls == {"stop-monitoring": 5, "general-message": 1}

    caso.paso2()
    e1 = _tablero(client)["legs"][2]["departures"][0]
    assert e1["platform"] == "11" and e1["platform_new"] is True


# ---------------- PRIM que falla: R26, R65, R66, R67 ----------------

def test_R26_una_estacion_caida_no_tumba_el_tablero(client, fake_prim):
    _montar(client, S.estacion_caida(fake_prim))
    body = _tablero(client)
    assert body["errors"] == ["Victor Basch: tiempo de espera agotado"]
    assert [d["minutes"] for d in body["legs"][0]["departures"]] == [4]
    assert body["legs"][1]["departures"] == []


def test_R65_si_prim_cae_se_sirve_la_copia_con_su_edad(client, fake_prim):
    _montar(client, S.sin_via(fake_prim))
    assert _deps(_tablero(client))
    fake_prim.fail["*"] = 500
    _envejecer(200)
    body = _tablero(client)
    assert body["errors"] == []                 # hay copia: no es un fallo visible
    assert len(_deps(body)) == 2
    assert 199 <= body["legs"][0]["age"] <= 205
    assert body["stale"] is True and body["data_age"] >= 199
    assert body["last_error"] in ("stop-monitoring: HTTP 500", "general-message: HTTP 500")


def test_R66_peticiones_simultaneas_una_sola_llamada(client, fake_prim):
    from app import prim
    fake_prim.delay = 0.05

    async def cinco_a_la_vez():
        c = prim.get_client()
        return await asyncio.gather(*[c.stop_monitoring("STIF:StopArea:SP:71370:")
                                      for _ in range(5)])

    res = client.portal.call(cinco_a_la_vez)
    assert len(res) == 5 and fake_prim.calls["stop-monitoring"] == 1
    assert prim.get_client()._locks == {}       # los locks no se quedan


def test_R67_cada_estacion_a_su_ritmo(client, fake_prim):
    """Con el tren a 2 min la estacion se pide a los 20 s; con el bus a
    106 min, cada 10 min."""
    from app import board
    S.fijar_reloj(fake_prim)
    fake_prim.add(S.SAINT_LAZARE[0], S.Dep("C01739", "Ermont - Eaubonne", 2))
    fake_prim.add(S.MARSEILLAISE[0], S.Dep("C00306", "Pont de Bezons", 106, aimed_delta=0))
    _montar(client, S.Caso("ritmo", S.ruta("Dos ritmos", [
        S.tramo(S.J, S.SAINT_LAZARE, S.ARGENTEUIL, ["Ermont - Eaubonne"]),
        S.tramo(S.BUS_6424, S.MARSEILLAISE, S.PONT_BEZONS, ["Pont de Bezons"])]), fake_prim))
    _tablero(client)
    assert board.station_ttl("stop_area:IDFM:71370") == 20
    assert board.station_ttl("stop_area:IDFM:490001") == 600
    assert fake_prim.calls["stop-monitoring"] == 2

    _envejecer(25)
    _tablero(client)
    assert fake_prim.calls["stop-monitoring"] == 3      # solo Saint-Lazare
    assert fake_prim.calls["general-message"] == 1      # 150 s de TTL


# ---------------- errores de PRIM ----------------

ESPERADO = {
    401: ("clave no válida (HTTP 401)", 503, "PRIM rechaza la clave"),
    403: ("sin permiso para esta API (HTTP 403)", 503, "PRIM rechaza la clave"),
    429: ("cuota agotada (HTTP 429)", 503, "cuota diaria de PRIM agotada"),
    500: ("HTTP 500", 502, "no responde: HTTP 500"),
    "timeout": ("tiempo de espera agotado", 502, "no responde"),
}


@pytest.mark.parametrize("fallo", S.FALLOS)
def test_errores_de_prim_con_mensaje_limpio(client, fake_prim, fallo):
    """Cada fallo llega al pie del tablero con un texto corto, sin URL ni
    parametros, y el buscador responde con su codigo."""
    from app import prim
    texto, status, detalle = ESPERADO[fallo]
    _montar(client, S.error_prim(fake_prim, fallo))
    body = _tablero(client)
    assert sorted(body["errors"]) == [f"Gare Saint-Lazare: {texto}", f"avisos: {texto}"]
    assert body["legs"][0]["departures"] == []
    assert body["last_error"].endswith(texto)
    assert "prim.iledefrance" not in str(body) and "MonitoringRef" not in str(body)

    r = client.get("/api/search/stops", params={"q": "argenteuil"})
    assert r.status_code == status and detalle in r.json()["detail"]
    assert "argenteuil" not in (prim.get_client().last_error or "")

    estado = prim.server_state()
    if fallo == 401:
        assert estado["prim_key"] == "invalid"
    elif fallo == 403:
        assert estado["prim_key"] == "forbidden"
    elif fallo == 429:
        assert estado["quota_level"] == "exhausted" and estado["refresh_hint_s"] == 300
    else:
        assert estado["degraded"] is True
