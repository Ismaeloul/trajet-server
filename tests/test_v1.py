"""API v1 del iPhone de principio a fin, con el PRIM falso.

Cada respuesta pasa por ContractClient: si no cumple docs/openapi.yaml para
su operacion y su codigo de estado, el test falla aunque no lo mire nadie.
"""
from __future__ import annotations

import pytest
import scenarios as S
from _seg_contrato import ContractClient, auth_limpio, pair  # noqa: F401
from conftest import ruta_j
from fakeprim import Dep, Msg
from fastapi.testclient import TestClient

from app import translate
from app.config import settings

J_LEG = S.tramo(S.J, S.SAINT_LAZARE, S.ARGENTEUIL, ["Ermont - Eaubonne"])


@pytest.fixture
def api(client):
    """Cliente con un iPhone ya emparejado (token en cada peticion)."""
    v1 = ContractClient(client)
    return v1.with_token(pair(v1)["token"])


@pytest.fixture
def sin_traductor(monkeypatch):
    """Hay Ollama configurado pero la traduccion nunca llega a tiempo: el
    tablero sale en frances con `translating` (R8, R80). Sin red: la tarea de
    fondo no hace nada."""
    async def _nada(text: str) -> None:
        translate._en_curso.pop(translate._key(text), None)

    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid:11434")
    monkeypatch.setattr(translate, "_background", _nada)


def _journeys() -> dict:
    """Un itinerario Navitia real de forma: andar + J + andar."""
    return {"journeys": [{
        "type": "best", "duration": 1860, "nb_transfers": 0,
        "departure_date_time": "20260924T083100", "arrival_date_time": "20260924T090200",
        "sections": [
            {"type": "street_network", "duration": 300},
            {"type": "public_transport", "duration": 1260,
             "departure_date_time": "20260924T083600",
             "display_informations": {"code": "J", "label": "J", "name": "J",
                                      "commercial_mode": "Train", "color": "CEC73D",
                                      "direction": "Ermont - Eaubonne (Eaubonne)"},
             "links": [{"type": "line", "id": "line:IDFM:C01739"}],
             "from": {"stop_point": {"name": "Gare Saint-Lazare", "stop_area": {
                 "id": "stop_area:IDFM:71370", "name": "Gare Saint-Lazare"}}},
             "to": {"stop_point": {"name": "Argenteuil", "stop_area": {
                 "id": "stop_area:IDFM:65063", "name": "Argenteuil"}}}},
            {"type": "street_network", "duration": 300},
        ]}]}


# ---------------- flujo completo ----------------

def test_flujo_completo_emparejar_y_tablero(client, fake_prim, sin_traductor):
    """Emparejar -> usar el token -> tablero con los casos de PreviewData:
    via real, metro sin via (platform_expected false), bus a 106 min, linea
    cortada y aviso en frances con «traduciendo»."""
    v1 = ContractClient(client)
    assert v1.get("/api/v1/ping").json()["paired"] is False
    res = pair(v1, "iPhone de Isma")
    api = v1.with_token(res["token"])
    assert api.get("/api/v1/ping").json()["paired"] is True
    me = api.get("/api/v1/devices/me").json()
    assert me["name"] == "iPhone de Isma" and "last_ip" not in me

    bus = S.bus_106(fake_prim)
    S.tren_en_anden(fake_prim)                     # metro 13 en el anden, sin via
    S.linea_cortada(fake_prim)                     # ... y cortada
    pantin = S.aviso_frances(fake_prim)            # bus 147 con aviso sin traducir
    fake_prim.add(S.SAINT_LAZARE[0],
                  Dep("C01739", "Ermont - Eaubonne", 6, aimed_delta=2, platform="21",
                      train="135711", length="longTrain"))
    ruta = S.ruta("Casa → Trabajo", [
        J_LEG,
        S.tramo(S.M13, S.SAINT_LAZARE, ("", ""), []),
        bus.ruta["legs"][0],
        pantin.ruta["legs"][0],
    ])
    r = api.post("/api/v1/routes", json=ruta)
    assert r.status_code == 201
    rid = r.json()["id"]

    r = api.get("/api/v1/board")
    assert r.status_code == 200
    b = r.json()
    assert b["route"]["id"] == rid and b["auto_selected"] is True
    assert "_all_failed" not in b and "_error" not in b
    assert b["server"]["prim_key"] == "valid" and b["disruptions_ok"] is True
    assert b["errors"] == []
    j, m13, bus6424, bus147 = b["legs"]

    # Via real en la J (tren: se espera via)
    assert j["platform_expected"] is True
    assert j["departures"][0]["platform"] == "21" and j["departures"][0]["delay"] == 2
    assert j["departures"][0]["length"] == "long"
    assert j["from_id"] == "stop_area:IDFM:71370" and j["to_id"] == "stop_area:IDFM:65063"

    # Metro: sin via y sin hueco para ella (R3); cortado (R68)
    assert m13["platform_expected"] is False
    assert m13["departures"] and all(d["platform"] is None for d in m13["departures"])
    assert m13["departures"][0]["at_stop"] is True
    assert m13["status"]["level"] == 2 and m13["status"]["label"] == "interrumpida"
    assert m13["status"]["messages"] == [S.AVISO_13_CORTADA]

    # Bus a 106 min (la app lo pinta «1h46», R6); sin via
    assert bus6424["platform_expected"] is False
    assert [d["minutes"] for d in bus6424["departures"]][-1] in (105, 106)

    # Aviso en frances, sin traducir todavia: se ve el frances y «traduciendo»
    st = bus147["status"]
    assert st["label"] == "perturbada" and st["messages"] == [S.AVISO_147]
    assert st["messages_es"] == [None] and st["translating"] is True
    assert m13["status"]["translating"] is True

    assert b["worst_level"] == 2 and b["worst_line"] == "13"
    # R64: una llamada por estacion (la J y el metro comparten Saint-Lazare)
    assert fake_prim.calls["stop-monitoring"] == 3


def test_tablero_vacio_sin_rutas(api):
    r = api.get("/api/v1/board")
    assert r.status_code == 200
    body = r.json()
    assert body["empty"] is True and body["server"]["prim_key"] in ("valid", "unknown")


def test_tablero_ruta_inexistente_404(api):
    api.post("/api/v1/routes", json=ruta_j())
    r = api.get("/api/v1/board", params={"route_id": 999})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_tablero_parcial_da_200_con_errors(api, fake_prim):
    """Si falla solo una estacion no es un tablero hueco: 200 y el fallo al pie."""
    caso = S.estacion_caida(fake_prim)
    api.post("/api/v1/routes", json=caso.ruta)
    r = api.get("/api/v1/board")
    assert r.status_code == 200
    body = r.json()
    assert any(e.startswith("Victor Basch:") for e in body["errors"])
    assert body["legs"][0]["departures"]


# ---------------- nunca un tablero hueco (R88, R9) ----------------

@pytest.mark.parametrize("fallo,status,code", [
    (401, 503, "prim_key_invalid"),
    (403, 503, "prim_key_invalid"),
    (429, 503, "prim_quota_exhausted"),
    (500, 502, "upstream"),
    ("timeout", 502, "prim_unreachable"),
    ("connect", 502, "prim_unreachable"),
])
def test_v1_board_nunca_hueco(api, fake_prim, fallo, status, code):
    """PRIM caido sin nada en cache: error disenado, nunca un tablero hueco."""
    caso = S.error_prim(fake_prim, fallo)
    api.post("/api/v1/routes", json=caso.ruta)
    r = api.get("/api/v1/board", params={"log_history": "false"})
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code and err["message"]
    assert "legs" not in r.json()


def test_tablero_sin_clave_prim_key_missing(env, fake_prim, monkeypatch):
    """Sin clave PRIM (ni en el panel ni en el entorno): 503 prim_key_missing
    y no se llama a PRIM."""
    from app.main import create_app

    monkeypatch.setenv("PRIM_API_KEY", "")
    settings.reload()
    with TestClient(create_app()) as tc:
        v1 = ContractClient(tc)
        api = v1.with_token(pair(v1)["token"])
        api.post("/api/v1/routes", json=ruta_j())
        r = api.get("/api/v1/board")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "prim_key_missing"
        h = api.get("/api/v1/health").json()
        assert h["prim"]["key_state"] == "missing" and h["prim"]["key_source"] == "none"
        # Sin rutas la app ve el tablero vacio con el estado del servidor
        api.delete("/api/v1/routes/1")
        assert api.get("/api/v1/board").json()["server"]["prim_key"] == "missing"
    assert fake_prim.total_calls() == 0


# ---------------- ETag, 304 y gzip ----------------

def test_etag_y_304(api):
    r = api.get("/api/v1/routes")
    etag = r.headers["etag"]
    assert etag.startswith('W/"')
    r2 = api.get("/api/v1/routes", headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b""
    rid = api.post("/api/v1/routes", json=ruta_j()).json()["id"]
    r3 = api.get("/api/v1/routes", headers={"If-None-Match": etag})
    assert r3.status_code == 200 and r3.headers["etag"] != etag
    # Y el mapa de la ruta (las tres operaciones con 304 en el contrato)
    e = api.get(f"/api/v1/routes/{rid}/map").headers["etag"]
    r4 = api.get(f"/api/v1/routes/{rid}/map", headers={"If-None-Match": e})
    assert r4.status_code in (200, 304)          # 200 si el mapa cambio (pending)


def test_gzip(api, fake_prim):
    S.bus_106(fake_prim)
    caso = S.casa_trabajo(fake_prim)
    api.post("/api/v1/routes", json=caso.ruta)
    r = api.get("/api/v1/board", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["etag"].startswith('W/"')
    assert r.json()["legs"]


# ---------------- rutas ----------------

def test_rutas_crear_editar_borrar(api):
    r = api.post("/api/v1/routes", json=ruta_j())
    assert r.status_code == 201
    rid = r.json()["id"]
    assert api.get(f"/api/v1/routes/{rid}").json()["name"] == ruta_j()["name"]
    lista = api.get("/api/v1/routes").json()
    assert lista["active_id"] == rid and len(lista["routes"]) == 1

    r = api.put(f"/api/v1/routes/{rid}", json=ruta_j(name="Otra", days=[0, 1]))
    assert r.status_code == 200
    assert r.json()["route"]["name"] == "Otra" and r.json()["route"]["days"] == [0, 1]

    assert api.delete(f"/api/v1/routes/{rid}").json() == {"deleted": rid}
    for method in ("get", "delete"):
        r = api.request(method, f"/api/v1/routes/{rid}")
        assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = api.put(f"/api/v1/routes/{rid}", json=ruta_j())
    assert r.status_code == 404


@pytest.mark.parametrize("cambio,texto", [
    ({"legs": []}, "tramo"),
    ({"name": "  "}, "name"),
    ({"days": [7]}, "days"),
    ({"time_from": "25:00"}, "time_from"),
    ({"legs": [{"line_id": "line:IDFM:C01739"}]}, "from_id"),
])
def test_rutas_validacion_400(api, cambio, texto):
    r = api.post("/api/v1/routes", json=ruta_j(**cambio))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "bad_request" and texto in err["message"]


def test_rutas_cuerpo_no_json_400(api):
    r = api.post("/api/v1/routes", content=b"no es json",
                 headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = api.post("/api/v1/routes", json=["lista"])
    assert r.status_code == 400
    r = api.post("/api/v1/routes")
    assert r.status_code == 400


def test_route_from_plan(api, fake_prim):
    fake_prim.add(S.SAINT_LAZARE[0], Dep("C01739", "Ermont - Eaubonne", 4))
    fake_prim.journeys_payload = _journeys()
    plan = api.get("/api/v1/plan", params={"from": "2.2170;48.9270",
                                           "to": "stop_area:IDFM:65063"})
    assert plan.status_code == 200
    option = plan.json()["options"][0]
    r = api.post("/api/v1/routes/from-plan",
                 json={"option": option, "meta": {"name": "Al curro", "days": [0, 1, 2, 3, 4],
                                                  "time_mode": "arrival", "time_at": "09:00"}})
    assert r.status_code == 201
    body = r.json()
    assert body["route"]["legs"][0]["directions"] == ["Ermont - Eaubonne"]
    assert body["without_direction"] == []
    assert body["route"]["time_mode"] == "arrival" and body["route"]["duration_min"] == 31
    r = api.post("/api/v1/routes/from-plan", json={"option": {"legs": []}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_mapa_de_la_ruta(api):
    rid = api.post("/api/v1/routes", json=ruta_j()).json()["id"]
    r = api.get(f"/api/v1/routes/{rid}/map")
    assert r.status_code == 200 and r.json()["route_id"] == rid
    assert api.get("/api/v1/routes/999/map").status_code == 404


# ---------------- planificador ----------------

@pytest.mark.parametrize("params", [
    {"when": "25:00"}, {"when": "8:30"}, {"when": "08:60"}, {"when": ""},
    {"mode": "later"}, {"from": "ab"}, {"to": ""},
])
def test_plan_parametros_malos_400(api, params):
    q = {"from": "2.2170;48.9270", "to": "stop_area:IDFM:65063", **params}
    r = api.get("/api/v1/plan", params=q)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_plan_ok_y_sin_trayecto(api, fake_prim):
    fake_prim.journeys_payload = _journeys()
    r = api.get("/api/v1/plan", params={"from": "2.2170;48.9270", "to": "stop_area:IDFM:65063",
                                        "when": "08:30", "mode": "arrival"})
    assert r.status_code == 200 and r.json()["options"][0]["legs"][0]["line_code"] == "J"
    fake_prim.journeys_payload = {"journeys": []}
    r = api.get("/api/v1/plan", params={"from": "2.2170;48.9270", "to": "2.30;48.80"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_plan_prim_caido(api, fake_prim):
    fake_prim.fail["navitia"] = "timeout"
    r = api.get("/api/v1/plan", params={"from": "2.2170;48.9270", "to": "stop_area:IDFM:65063"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "prim_unreachable"


# ---------------- buscadores ----------------

def _places(fake_prim):
    fake_prim.places["gare saint"] = [{
        "embedded_type": "stop_area", "name": "Gare Saint-Lazare (Paris)",
        "stop_area": {"id": "stop_area:IDFM:71370", "name": "Gare Saint-Lazare",
                      "administrative_regions": [{"name": "Paris"}],
                      "lines": [{"id": "line:IDFM:C01739", "code": "J",
                                 "commercial_mode": {"name": "Train"}, "color": "CEC73D"}]}}]
    fake_prim.places["marseillaise"] = [{
        "embedded_type": "address", "name": "6 Rue de la Marseillaise (Argenteuil)",
        "address": {"id": "2.2170;48.9270", "name": "6 Rue de la Marseillaise",
                    "administrative_regions": [{"name": "Argenteuil"}]}}]
    fake_prim.lines_at["stop_area:IDFM:71370"] = [
        {"id": "line:IDFM:C00306", "code": "6424", "name": "6424",
         "commercial_mode": {"name": "Bus"}, "color": "A50034"},
        {"id": "line:IDFM:C01739", "code": "J", "name": "J",
         "commercial_mode": {"name": "Train"}, "color": "CEC73D"},
        {"id": "line:IDFM:C01383", "code": "13", "name": "13",
         "commercial_mode": {"name": "Métro"}, "color": "82C8E6"}]


def test_buscadores(api, fake_prim):
    _places(fake_prim)
    r = api.get("/api/v1/search/stops", params={"q": "  Gare Saint "})
    assert r.status_code == 200 and r.json()["stops"][0]["id"] == "stop_area:IDFM:71370"
    r = api.get("/api/v1/search/places", params={"q": "Marseillaise"})
    assert r.json()["places"][0]["kind"] == "dirección"
    r = api.get("/api/v1/stops/stop_area:IDFM:71370/lines")
    assert [ln["code"] for ln in r.json()["lines"]] == ["13", "J", "6424"]   # R62
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4), Dep("C01739", "Gisors", 8))
    r = api.get("/api/v1/stops/stop_area:IDFM:71370/directions",
                params={"line_id": "line:IDFM:C01739"})
    assert sorted(r.json()["directions"]) == ["Ermont - Eaubonne", "Gisors"]


@pytest.mark.parametrize("url,params", [
    ("/api/v1/search/stops", {"q": "a"}),
    ("/api/v1/search/stops", {"q": " a "}),
    ("/api/v1/search/stops", {"q": "x" * 81}),
    ("/api/v1/search/stops", {}),
    ("/api/v1/search/places", {"q": "b"}),
    ("/api/v1/search/places", {"q": "y" * 81}),
    ("/api/v1/stops/stop_area:IDFM:71370/directions", {}),
])
def test_buscadores_400(api, url, params):
    r = api.get(url, params=params)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_buscador_prim_caido(api, fake_prim):
    fake_prim.fail["navitia"] = 401
    r = api.get("/api/v1/search/stops", params={"q": "gare"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "prim_key_invalid"


# ---------------- alternativas ----------------

def test_alternativas(api, fake_prim):
    rid = api.post("/api/v1/routes", json=ruta_j()).json()["id"]
    r = api.get(f"/api/v1/alternatives/{rid}")
    assert r.json() == {"needed": False, "affected": [], "options": []}
    fake_prim.journeys_payload = _journeys()
    fake_prim.message(Msg(["C01739"], "Trafic interrompu entre Saint-Lazare et Houilles."))
    from app import prim
    prim.get_client().clear_cache()
    r = api.get(f"/api/v1/alternatives/{rid}")
    body = r.json()
    assert body["needed"] is True and body["affected"][0]["label"] == "interrumpida"
    assert body["options"][0]["usable"] is False            # la J esta cortada (R30)
    assert api.get("/api/v1/alternatives/999").status_code == 404


# ---------------- estadisticas y salud ----------------

@pytest.mark.parametrize("days,status", [(1, 200), (90, 200), (3650, 200),
                                          (0, 400), (3651, 400), (-5, 400), ("x", 400)])
def test_stats_days(api, days, status):
    r = api.get("/api/v1/stats", params={"days": days})
    assert r.status_code == status


def test_stats_por_defecto_y_platform_model(api):
    assert api.get("/api/v1/stats").status_code == 200
    rid = api.post("/api/v1/routes", json=ruta_j()).json()["id"]
    r = api.get("/api/v1/platform-model")
    assert r.status_code == 200 and r.json()["route"]["id"] == rid
    assert "coverage" not in api.get("/api/v1/platform-model", params={"route_id": 999}).json()


def test_health(api):
    r = api.get("/api/v1/health")
    assert r.status_code == 200
    h = r.json()
    assert h["ok"] is True and h["api"] == 1 and h["version"] == settings_version()
    assert h["schema_version"] >= 2
    assert h["prim"]["key_state"] in ("valid", "unknown") and h["prim"]["key_source"] == "env"


def settings_version() -> str:
    from app.config import VERSION
    return VERSION
