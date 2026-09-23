"""La API v1 de la app es la del contrato (docs/openapi.yaml, congelado).

- Cada operacion de /api/v1 del contrato existe en la app con el mismo
  metodo, los mismos parametros de ruta y de query y el mismo operationId (y
  la app no tiene ninguna de mas).
- Cada respuesta que dan los tests (tambien las de error) valida con el
  esquema del contrato para su codigo de estado: lo hace ContractClient en
  _seg_contrato.py, que usan test_auth.py, test_v1.py y este fichero. Aqui
  ademas se recorren TODAS las operaciones y se comprueba que no falta
  ninguna por ejercitar.
"""
from __future__ import annotations

import pytest
import scenarios as S
from _seg_contrato import (ContractClient, auth_limpio, check_response, pair,  # noqa: F401
                           sample_path, schema_validator, spec, v1_operations)
from conftest import ruta_j
from fakeprim import Dep, Msg

from app.api.errors import STATUS


def _app_ops(app) -> dict[tuple[str, str], dict]:
    out = {}
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                out[(path, method)] = op
    return out


def _params(op: dict, extra: list | None = None) -> set:
    """(nombre, sitio, obligatorio) de los parametros de ruta y de query.

    Las cabeceras (If-None-Match) las pone el middleware, no la operacion."""
    out = set()
    for p in list(op.get("parameters", [])) + list(extra or []):
        if "$ref" in p or p.get("in") not in ("path", "query"):
            continue
        out.add((p["name"], p["in"], bool(p.get("required"))))
    return out


def test_operaciones_v1_existen_con_el_mismo_metodo(app):
    contrato = {(p, m) for p, m, _ in v1_operations()}
    assert len(contrato) == 21
    assert set(_app_ops(app)) == contrato


def test_parametros_y_operation_id_como_el_contrato(app):
    ops = _app_ops(app)
    for path, method, op in v1_operations():
        mio = ops[(path, method)]
        assert _params(mio) == _params(op, op["_path_params"]), f"{method.upper()} {path}"
        assert mio["operationId"] == op["operationId"], f"{method.upper()} {path}"


def test_codigos_de_error_del_contrato():
    """Los codigos de ErrorV1 que lista el contrato tienen su estado HTTP."""
    desc = spec()["components"]["schemas"]["ErrorV1"]["description"]
    for code in ("bad_request", "unauthorized", "forbidden", "not_found", "pairing_invalid",
                 "rate_limited", "prim_key_missing", "prim_key_invalid",
                 "prim_quota_exhausted", "prim_unreachable", "upstream",
                 "prim_key_rejected", "internal"):
        assert code in desc and code in STATUS


def _journeys() -> dict:
    return {"journeys": [{
        "type": "best", "duration": 1500, "nb_transfers": 0,
        "departure_date_time": "20260924T083100", "arrival_date_time": "20260924T085600",
        "sections": [{
            "type": "public_transport", "duration": 1500,
            "departure_date_time": "20260924T083100",
            "display_informations": {"code": "J", "label": "J", "name": "J",
                                     "commercial_mode": "Train", "color": "CEC73D",
                                     "direction": "Ermont - Eaubonne"},
            "links": [{"type": "line", "id": "line:IDFM:C01739"}],
            "from": {"stop_point": {"stop_area": {"id": "stop_area:IDFM:71370",
                                                  "name": "Gare Saint-Lazare"}}},
            "to": {"stop_point": {"stop_area": {"id": "stop_area:IDFM:65063",
                                                "name": "Argenteuil"}}}}]}]}


def test_todas_las_operaciones_responden_segun_el_contrato(client, fake_prim):
    """Recorre las 21 operaciones (bien y mal) y todas validan."""
    v1 = ContractClient(client)
    caso = S.casa_trabajo(fake_prim)
    fake_prim.add(S.SAINT_LAZARE[0], Dep("C01739", "Ermont - Eaubonne", 4, platform="3"))
    fake_prim.journeys_payload = _journeys()
    fake_prim.places["gare"] = [{"embedded_type": "stop_area", "name": "Gare Saint-Lazare",
                                 "stop_area": {"id": "stop_area:IDFM:71370", "lines": []}}]
    fake_prim.lines_at["stop_area:IDFM:71370"] = [
        {"id": "line:IDFM:C01739", "code": "J", "name": "J",
         "commercial_mode": {"name": "Train"}, "color": "CEC73D"}]

    v1.get("/api/v1/ping")
    v1.post("/api/v1/pair", json={"code": "ABCD-EFGH", "device_name": "x"})      # 401
    v1.post("/api/v1/pair", json={"code": "x"})                                   # 400
    api = v1.with_token(pair(v1)["token"])

    api.get("/api/v1/devices/me")
    api.get("/api/v1/health")
    api.get("/api/v1/board")                                                      # vacio
    rid = api.post("/api/v1/routes", json=caso.ruta).json()["id"]
    api.post("/api/v1/routes", json={"name": "sin tramos"})                       # 400
    api.get("/api/v1/routes")
    api.get(f"/api/v1/routes/{rid}")
    api.get("/api/v1/routes/999")                                                 # 404
    api.put(f"/api/v1/routes/{rid}", json=caso.ruta)
    api.put("/api/v1/routes/999", json=caso.ruta)                                 # 404
    api.get(f"/api/v1/routes/{rid}/map")
    api.get("/api/v1/board", params={"route_id": rid})
    api.get("/api/v1/board", params={"route_id": 999})                            # 404
    api.get(f"/api/v1/alternatives/{rid}", params={"force": "true"})
    api.get("/api/v1/alternatives/999")                                           # 404
    api.get("/api/v1/search/stops", params={"q": "gare"})
    api.get("/api/v1/search/stops", params={"q": "g"})                            # 400
    api.get("/api/v1/search/places", params={"q": "gare"})
    api.get("/api/v1/stops/stop_area:IDFM:71370/lines")
    api.get("/api/v1/stops/stop_area:IDFM:71370/directions", params={"line_id": "line:IDFM:C01739"})
    api.get("/api/v1/stops/stop_area:IDFM:71370/directions")                     # 400
    plan = api.get("/api/v1/plan", params={"from": "2.2170;48.9270", "to": "stop_area:IDFM:65063"})
    api.get("/api/v1/plan", params={"from": "2.2170;48.9270", "to": "x" * 5, "when": "24:00"})
    api.post("/api/v1/routes/from-plan", json={"option": plan.json()["options"][0]})
    api.post("/api/v1/routes/from-plan", json={"meta": {}})                       # 400
    api.get("/api/v1/stats")
    api.get("/api/v1/stats", params={"days": 0})                                  # 400
    api.get("/api/v1/platform-model")
    api.get("/api/v1/platform-model", params={"route_id": rid})
    api.delete(f"/api/v1/routes/{rid}")
    api.delete(f"/api/v1/routes/{rid}")                                           # 404
    # PRIM caido: Navitia no responde (502) y SIRI rechaza la clave (503)
    from app import prim
    prim.get_client().clear_cache()
    fake_prim.fail["navitia"] = "timeout"
    api.get("/api/v1/search/places", params={"q": "otra cosa"})                   # 502
    fake_prim.fail["stop-monitoring"] = fake_prim.fail["general-message"] = 401
    api.post("/api/v1/routes", json=ruta_j())
    assert api.get("/api/v1/board").status_code == 503
    api.delete("/api/v1/devices/me")
    api.get("/api/v1/health")                                                     # 401

    ejercitadas = {(p, m) for m, p, _ in v1.seen}
    faltan = {(p, m) for p, m, _ in v1_operations()} - ejercitadas
    assert not faltan, f"operaciones sin ejercitar: {sorted(faltan)}"
    estados = {s for _, _, s in v1.seen}
    assert {"200", "201", "400", "401", "404", "502", "503"} <= estados


def test_429_y_retry_after_segun_el_contrato(client):
    v1 = ContractClient(client)
    for _ in range(5):
        v1.post("/api/v1/pair", json={"code": "ABCD-EFGH", "device_name": "x"})
    r = v1.post("/api/v1/pair", json={"code": "ABCD-EFGH", "device_name": "x"})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_board_v1_forma_con_todos_los_casos(client, fake_prim):
    """Cada escenario de PreviewData por /api/v1/board valida con BoardV1."""
    v1 = ContractClient(client)
    api = v1.with_token(pair(v1)["token"])
    # La estacion que no responde, al final: tras un tiempo de espera PRIM
    # deja ese endpoint en pausa unos segundos (prim.py) y el resto fallaria.
    orden = sorted(S.ESCENARIOS.items(), key=lambda kv: kv[0] == "estacion_caida")
    for nombre, fabrica in orden:
        fake_prim.stations.clear()
        fake_prim.messages.clear()
        fake_prim.fail_stations.clear()
        from app import prim
        prim.get_client().clear_cache()
        caso = fabrica(fake_prim)
        rid = api.post("/api/v1/routes", json=caso.ruta).json()["id"]
        r = api.get("/api/v1/board", params={"route_id": rid, "log_history": "false"})
        assert r.status_code == 200, (nombre, r.text)
        if caso.paso2:
            caso.paso2()
            r = api.get("/api/v1/board", params={"route_id": rid, "log_history": "false"})
            assert r.status_code == 200, (nombre, r.text)
        api.delete(f"/api/v1/routes/{rid}")


@pytest.mark.parametrize("nombre", ["ErrorV1", "Ping", "PairResult", "Device",
                                    "PairingSession", "PairingStatus", "HealthV1",
                                    "BoardV1", "BoardEmptyV1", "ErrorLogEntry"])
def test_esquemas_del_contrato_compilan(nombre):
    schema_validator(nombre).check_schema(spec()["components"]["schemas"][nombre])


def test_check_response_detecta_lo_que_no_cumple():
    """El vigilante del contrato falla de verdad (si no, todo lo demas no vale)."""
    class Falsa:
        def __init__(self, status, body, ctype="application/json"):
            import json
            self.status_code = status
            self.content = json.dumps(body).encode()
            self.text = self.content.decode()
            self.headers = {"content-type": ctype}

        def json(self):
            import json
            return json.loads(self.content)

    check_response("get", "/api/v1/ping", Falsa(200, {"ok": True, "service": "trajet", "api": 1,
                                                     "version": "0.4.0", "paired": False}))
    with pytest.raises(AssertionError):
        check_response("get", "/api/v1/ping", Falsa(200, {"ok": True}))
    with pytest.raises(AssertionError):                         # estado no declarado
        check_response("get", "/api/v1/ping", Falsa(401, {"error": {"code": "x", "message": "y"}}))
    with pytest.raises(AssertionError):                         # campo de mas
        check_response("get", "/api/v1/devices/me", Falsa(401, {"error": {
            "code": "unauthorized", "message": "m", "extra": 1}}))
    with pytest.raises(AssertionError):                         # fecha sin zona
        check_response("get", "/api/v1/devices/me", Falsa(200, {
            "id": 1, "name": "x", "model": "", "created_at": "2026-09-24 08:00:00",
            "last_used_at": None}))
    assert sample_path("/api/v1/routes/{route_id}/map") == "/api/v1/routes/1/map"


def test_aviso_traducido_y_planned_segun_el_contrato(client, fake_prim):
    """messages_es con traduccion y avisos futuros en `planned` (R28, R69)."""
    v1 = ContractClient(client)
    api = v1.with_token(pair(v1)["token"])
    caso = S.casa_trabajo(fake_prim)
    fake_prim.message(Msg(["C01739"], S.aviso_futuro(10)))
    api.post("/api/v1/routes", json=caso.ruta)
    b = api.get("/api/v1/board").json()
    bus147 = b["legs"][4]["status"]
    assert bus147["messages_es"] == [S.AVISO_147_ES] and bus147["translating"] is False
    assert b["legs"][2]["status"]["planned"] >= 1 and b["legs"][2]["status"]["level"] == 0
