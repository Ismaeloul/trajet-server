"""La API de la 0.3.0 (/api/*) sigue funcionando igual.

- El conjunto (ruta, metodo, parametros) es el de produccion.
- Las respuestas cumplen los esquemas ESTRICTOS de docs/openapi-0.3.0.yaml.
"""
from __future__ import annotations

import json
import os

import pytest
import yaml
from conftest import ruta_j
from fakeprim import Dep, Msg
from jsonschema import Draft202012Validator

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contract")


def _spec_030() -> dict:
    with open(os.path.join(DOCS, "openapi-0.3.0.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validator(spec: dict, name: str) -> Draft202012Validator:
    schema = {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
    return Draft202012Validator(schema)


def _ops(openapi: dict, prefix_filter=lambda p: True) -> set:
    out = set()
    for path, item in openapi["paths"].items():
        if not prefix_filter(path):
            continue
        for method, op in item.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            params = tuple(sorted((p["name"], p["in"], bool(p.get("required")))
                                  for p in op.get("parameters", [])))
            out.add((path, method, params))
    return out


def test_mismas_rutas_y_parametros_que_produccion(app):
    """Las 15 operaciones /api/* de la 0.3.0 siguen, con los mismos parametros."""
    with open(os.path.join(DOCS, "openapi-live-0.3.0.json"), encoding="utf-8") as f:
        live = json.load(f)
    legacy = lambda p: p.startswith("/api/") and not p.startswith(("/api/v1", "/api/admin"))  # noqa: E731
    assert _ops(app.openapi(), legacy) == _ops(live, legacy)


def test_health_legacy(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    _validator(_spec_030(), "Health").validate(r.json())


def test_rutas_crud_legacy(client):
    spec = _spec_030()
    r = client.post("/api/routes", json=ruta_j())
    assert r.status_code == 200, r.text
    _validator(spec, "RouteSaved").validate(r.json())
    rid = r.json()["id"]
    r = client.get("/api/routes")
    _validator(spec, "RouteList").validate(r.json())
    assert r.json()["active_id"] == rid
    r = client.put(f"/api/routes/{rid}", json=ruta_j(name="Otra"))
    assert r.json()["route"]["name"] == "Otra"
    assert client.delete(f"/api/routes/{rid}").json() == {"deleted": rid}
    assert client.delete(f"/api/routes/{rid}").status_code == 404


def test_ruta_sin_nombres_ya_no_da_500(client):
    """En la 0.3.0 faltar origin_name daba 500 (KeyError)."""
    data = ruta_j()
    data.pop("origin_name")
    assert client.post("/api/routes", json=data).status_code == 200
    data["legs"] = []
    r = client.post("/api/routes", json=data)
    assert r.status_code == 400 and "tramo" in r.json()["detail"]


def test_tablero_vacio(client):
    assert client.get("/api/board").json() == {
        "empty": True, "message": "todavia no hay rutas guardadas"}


def test_tablero_legacy_forma_exacta(client, fake_prim):
    fake_prim.add("71370",
                  Dep("C01739", "Ermont - Eaubonne", 6, aimed_delta=2, platform="21",
                      train="135711", length="longTrain"),
                  Dep("C01739", "Ermont - Eaubonne", 21, aimed_delta=None),
                  Dep("C01739", "Gisors", 9))          # otro sentido: fuera
    fake_prim.message(Msg(["C01739"], "Trafic perturbé en raison d'un incident."))
    client.post("/api/routes", json=ruta_j())
    r = client.get("/api/board")
    assert r.status_code == 200
    body = r.json()
    _validator(_spec_030(), "Board").validate(body)
    deps = body["legs"][0]["departures"]
    assert [d["destination"] for d in deps] == ["Ermont - Eaubonne"] * 2
    assert deps[0]["platform"] == "21" and deps[0]["delay"] == 2 and deps[0]["length"] == "long"
    assert deps[1]["delay"] is None                     # R4: sin hora teorica no hay retraso
    assert body["legs"][0]["status"]["label"] == "perturbada"


def test_tablero_legacy_una_llamada_por_estacion(client, fake_prim):
    """R64: dos tramos desde la misma estacion = una llamada."""
    ruta = ruta_j()
    ruta["legs"].append(dict(ruta["legs"][0], line_id="line:IDFM:C01383", line_code="13",
                             line_mode="Métro", directions=[]))
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4),
                  Dep("C01383", "Châtillon Montrouge", 2, aimed_delta=None))
    client.post("/api/routes", json=ruta)
    client.get("/api/board")
    assert fake_prim.calls["stop-monitoring"] == 1


def test_avisos_caidos_se_dicen(client, fake_prim):
    """Si general-message falla, `errors` lo dice (antes: «normal» en silencio)."""
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4))
    fake_prim.fail["general-message"] = 500
    client.post("/api/routes", json=ruta_j())
    body = client.get("/api/board").json()
    assert any(e.startswith("avisos:") for e in body["errors"])
    assert body["legs"][0]["departures"]


def test_plan_hora_fuera_de_rango_da_400(client):
    r = client.get("/api/plan", params={"from": "stop_area:IDFM:71370", "to": "2.25;48.94",
                                        "when": "25:00"})
    assert r.status_code == 400


# ---------------- SEC-1: solo desde el proxy de Umbrel ----------------

def _legacy_ops(app, route_id: int) -> list[tuple[str, str]]:
    """(metodo, url de ejemplo) de cada operacion /api/* de la 0.3.0."""
    out = []
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/api/") or path.startswith(("/api/v1", "/api/admin")):
            continue
        url = (path.replace("{route_id}", str(route_id))
                   .replace("{stop_id}", "stop_area:IDFM:71370"))
        out += [(m, url) for m in item if m in ("get", "post", "put", "delete")]
    return out


def test_api_030_solo_desde_el_proxy_de_umbrel(app, fake_prim, monkeypatch):
    """SEC-1: la 0.3.0 no tiene token, se apoya en el login de Umbrel; otra
    app de la red Docker que llegue directa (10.21.0.7) se lo saltaria. Con
    TRAJET_ADMIN_PEERS=auto, como en Umbrel: 403 en TODO /api/* sin tocar
    nada ni gastar cuota. Por el proxy (la puerta de enlace) o 127.0.0.1
    funciona como siempre, sin la cabecera X-Trajet-Panel del panel."""
    from _seg_contrato import PeerApp
    from fastapi.testclient import TestClient

    from app import auth

    monkeypatch.setattr(auth.settings, "admin_peers", "auto")
    monkeypatch.setattr(auth, "_default_gateway", lambda: "10.21.0.1")
    auth.reset_state()
    with TestClient(PeerApp(app)) as c:
        proxy = {"x-test-peer": "10.21.0.1"}
        r = c.post("/api/routes", json=ruta_j(origin_id="2.3488;48.8534",
                                                origin_name="12 Rue de casa"), headers=proxy)
        assert r.status_code == 200, r.text
        rid = r.json()["id"]
        ops = _legacy_ops(app, rid)
        assert len(ops) == 15

        for peer in ("10.21.0.7", "192.168.1.40", "testclient"):
            # X-Forwarded-For no cuenta: manda la conexion
            h = {"x-test-peer": peer, "X-Forwarded-For": "10.21.0.1"}
            for method, url in ops:
                kw = {"json": ruta_j()} if method in ("post", "put") else {}
                r = c.request(method.upper(), url, headers=h, **kw)
                assert r.status_code == 403, (peer, method, url, r.text)
                assert "proxy de Umbrel" in r.json()["detail"]
                assert "12 Rue de casa" not in r.text
        assert fake_prim.total_calls() == 0

        # La ruta sigue ahi y, desde el proxy o 127.0.0.1, todo va.
        for peer in ("10.21.0.1", "127.0.0.1"):
            r = c.get("/api/routes", headers={"x-test-peer": peer})
            assert r.status_code == 200 and [x["id"] for x in r.json()["routes"]] == [rid]
        assert c.get("/api/health", headers=proxy).status_code == 200
        assert c.delete(f"/api/routes/{rid}", headers=proxy).json() == {"deleted": rid}


# ---------------- fallos de PRIM: 502 como siempre ----------------

@pytest.mark.parametrize("fallo", [401, 403, 429, 500, "timeout"])
def test_fallos_de_prim_502_como_en_la_030(client, fake_prim, make_route, fallo):
    """H9: en /api/* cualquier fallo de PRIM es 502 con el texto de
    siempre, tambien sin clave, con la clave rechazada o sin cuota (esos 503
    con codigo son de la v1). En las alternativas, «el calculador»."""
    texto = {401: "clave no válida (HTTP 401)", 403: "sin permiso para esta API (HTTP 403)",
             429: "cuota agotada (HTTP 429)", 500: "HTTP 500",
             "timeout": "tiempo de espera agotado"}[fallo]
    rid = make_route()
    fake_prim.message(Msg(["C01739"], "Trafic interrompu entre Saint-Lazare et Houilles."))
    fake_prim.fail["navitia"] = fallo
    if fallo == 429:
        fake_prim.remaining["navitia"] = 1           # la cuota del dia: se acaba
    casos = [("/api/search/stops", {"q": "gare"}, "la API de IDFM"),
             ("/api/search/places", {"q": "gare"}, "la API de IDFM"),
             ("/api/stops/stop_area:IDFM:71370/lines", {}, "la API de IDFM"),
             ("/api/plan", {"from": "2.2170;48.9270", "to": "stop_area:IDFM:65063"},
              "la API de IDFM"),
             (f"/api/alternatives/{rid}", {}, "el calculador")]
    for i, (url, params, quien) in enumerate(casos):
        r = client.get(url, params=params)
        assert r.status_code == 502, (url, r.text)
        # La primera llamada trae el motivo de PRIM; las siguientes, el de la
        # pausa tras el fallo (el mismo) o, con la cuota del dia agotada, ese.
        motivo = "cuota diaria agotada" if fallo == 429 and i else texto
        assert r.json() == {"detail": f"{quien} no responde: {motivo}"}, url
    assert fake_prim.calls["navitia"] == 1


def test_sin_clave_502_como_en_la_030(env, fake_prim, monkeypatch):
    """Sin clave de PRIM: la v1 dice prim_key_missing (503); la 0.3.0, 502."""
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import create_app

    monkeypatch.setenv("PRIM_API_KEY", "")
    settings.reload()
    with TestClient(create_app()) as c:
        r = c.get("/api/search/stops", params={"q": "gare"})
        assert r.status_code == 502
        assert r.json() == {"detail": "la API de IDFM no responde: sin clave de PRIM"}
    assert fake_prim.total_calls() == 0


@pytest.mark.parametrize("path", ["/api/stats", "/api/platform-model"])
def test_estadisticas_legacy(client, path):
    spec = _spec_030()
    r = client.get(path)
    assert r.status_code == 200
    _validator(spec, "Stats" if path.endswith("stats") else "PlatformModel").validate(r.json())
