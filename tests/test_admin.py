"""API del panel (/api/admin/*): contrato, clave PRIM, emparejamiento,
dispositivos, ajustes, cuota, errores y resumen (R83, R84, R86, R87).

Todo sin red: PRIM falso (tests/fakeprim.py) y la IP de la conexion con
PeerApp. Cada respuesta de /api/admin que sale en estos tests se valida
contra el contrato (tests/contract/openapi.yaml) para su operacion y su
codigo de estado: un estado que el contrato no declara tambien es un fallo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from contextlib import ExitStack
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from _seg_contrato import METHODS, PeerApp, auth_limpio, spec, validator  # noqa: F401
from conftest import FAKE_KEY, ruta_j
from fakeprim import Dep, Msg
from fastapi.testclient import TestClient

from app import auth, db, mapdata, prim
from app import platform as P
from app import translate as T
from app.api import admin as A

# Claves de mentira con la forma de las reales. Ninguna puede salir nunca del
# servidor (R83): solo sus 4 ultimos caracteres, en el panel.
NUEVA = "PANEL0nueva0clave0de0pruebas0Q7Zk"
OTRA = "PANEL0otra0clave0distinta0000R2d9"
MALA = "PANEL0clave0que0PRIM0rechaza0Xy12"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
ADMIN = "/api/admin/"

# Estados declarados que este servidor no produce nunca, con el motivo.
NO_PRODUCIDOS = {
    # POST /pairing no lleva cuerpo ni parametros: no hay nada que pueda ser
    # un 400. Se genera aunque no haya direcciones (el panel lo avisa).
    ("post", "/api/admin/pairing", "400"),
}


# ---------------- el contrato de /api/admin ----------------

def admin_operations() -> list[tuple[str, str, dict]]:
    out = []
    for path, item in spec()["paths"].items():
        if not path.startswith(ADMIN):
            continue
        for method in METHODS:
            if method in item:
                out.append((path, method, item[method]))
    return out


def _tpl_re(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$")


def find_admin(method: str, path: str) -> tuple[str, dict] | None:
    found = [(tpl.count("{"), tpl, op) for tpl, m, op in admin_operations()
             if m == method.lower() and _tpl_re(tpl).match(path)]
    if not found:
        return None
    found.sort(key=lambda x: x[0])
    return found[0][1], found[0][2]


def _response_decl(resp: dict) -> dict:
    ref = resp.get("$ref")
    return spec()["components"]["responses"][ref.rsplit("/", 1)[-1]] if ref else resp


def check_admin(method: str, path: str, r, sent_json=None) -> str:
    """Falla si la respuesta no es la que declara el contrato. Devuelve la
    plantilla de la ruta."""
    found = find_admin(method, path)
    assert found is not None, f"{method.upper()} {path} no esta en el contrato"
    tpl, op = found
    status = str(r.status_code)
    assert status in op["responses"], (
        f"{method.upper()} {tpl}: el contrato no declara {status} "
        f"(declara {sorted(op['responses'])}); cuerpo: {r.text[:300]}")
    decl = _response_decl(op["responses"][status])
    schema = decl["content"]["application/json"]["schema"]
    assert r.headers.get("content-type", "").startswith("application/json"), r.headers
    errs = sorted(validator(schema).iter_errors(r.json()), key=lambda e: list(e.absolute_path))
    assert not errs, (f"{method.upper()} {tpl} {status} no cumple el contrato:\n"
                      + "\n".join(f"  {list(e.absolute_path)}: {e.message[:300]}" for e in errs[:10]))
    # Todo /api/admin sin cache (lleva datos del servidor).
    assert r.headers.get("cache-control") == "no-store", r.headers.get("cache-control")
    # Lo que el servidor acepta tiene que ser un cuerpo valido segun el contrato.
    if sent_json is not None and r.status_code < 300 and "requestBody" in op:
        body_schema = op["requestBody"]["content"]["application/json"]["schema"]
        validator(body_schema).validate(sent_json)
    return tpl


class Admin:
    """TestClient que hace de panel: pone `X-Trajet-Panel: 1` en lo que
    modifica (salvo panel=False) y valida cada respuesta de /api/admin."""

    def __init__(self, client: TestClient, seen: set | None = None):
        self.client = client
        self.seen: set[tuple[str, str, str]] = set() if seen is None else seen

    @property
    def portal(self):
        return self.client.portal

    def request(self, method: str, url: str, *, panel: bool = True, **kw):
        headers = dict(kw.pop("headers", None) or {})
        if panel and method.upper() in UNSAFE:
            headers.setdefault("X-Trajet-Panel", "1")
        r = self.client.request(method.upper(), url, headers=headers, **kw)
        path = url.split("?", 1)[0]
        if path.startswith(ADMIN):
            tpl = check_admin(method, path, r, kw.get("json"))
            self.seen.add((method.lower(), tpl, str(r.status_code)))
        return r

    def get(self, url, **kw):
        return self.request("get", url, **kw)

    def post(self, url, **kw):
        return self.request("post", url, **kw)

    def put(self, url, **kw):
        return self.request("put", url, **kw)

    def patch(self, url, **kw):
        return self.request("patch", url, **kw)

    def delete(self, url, **kw):
        return self.request("delete", url, **kw)


@pytest.fixture
def arrancar(env, fake_prim, monkeypatch):
    """`arrancar(**entorno)` arranca el servidor con esas variables de entorno
    (ademas de las de conftest.env) y devuelve el cliente del panel."""
    from app.config import settings
    from app.main import create_app

    stack = ExitStack()

    def _start(**envvars) -> Admin:
        for k, v in envvars.items():
            monkeypatch.setenv(k, v)
        settings.reload()
        tc = stack.enter_context(TestClient(PeerApp(create_app())))
        return Admin(tc)

    yield _start
    stack.close()


@pytest.fixture
def admin(arrancar) -> Admin:
    return arrancar()


def _err(r, status: int, code: str, texto: str | None = None) -> dict:
    assert r.status_code == status, r.text
    body = r.json()
    assert body["error"]["code"] == code, body
    if texto:
        assert texto in body["error"]["message"], body
    return body


def _con_ruta(admin: Admin, fake_prim) -> None:
    """Una ruta y un PRIM con salidas, para ver con que clave llama el tablero."""
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6, platform="3"))
    fake_prim.message(Msg(["C01739"], "Trafic perturbé entre Paris et Argenteuil."))
    r = admin.client.post("/api/routes", json=ruta_j())
    assert r.status_code == 200, r.text


def _pair_device(admin: Admin, name: str = "iPhone de Isma") -> dict:
    """Como en la vida real: el panel genera el QR y el iPhone canjea el codigo."""
    s = admin.post("/api/admin/pairing")
    assert s.status_code == 201, s.text
    r = admin.client.post("/api/v1/pair", json={"code": s.json()["code"], "device_name": name,
                                                "device_model": "iPhone17,1", "app_version": "2.0"})
    assert r.status_code == 200, r.text
    return {"session": s.json(), **r.json()}


def _secretos() -> str:
    from app.config import settings
    return os.path.join(settings.data_dir, "secrets", "prim-key.json")


# =====================================================================
#  Contrato: cada operacion y cada estado declarado
# =====================================================================

def test_rutas_de_la_app_coinciden_con_el_contrato(app):
    """Cada operacion admin del contrato existe con su operationId."""
    generado = app.openapi()["paths"]
    for tpl, method, op in admin_operations():
        assert tpl in generado and method in generado[tpl], f"falta {method.upper()} {tpl}"
        assert generado[tpl][method]["operationId"] == op["operationId"]
    # Y no hay rutas de /api/admin que el contrato no conozca.
    propias = {(p, m) for p, item in generado.items() if p.startswith(ADMIN) for m in item}
    assert propias == {(p, m) for p, m, _ in admin_operations()}


def test_cada_estado_declarado_se_ejercita_y_cumple_el_contrato(admin, fake_prim, monkeypatch):
    """Todas las operaciones con todos sus estados (incluidos los errores),
    y cada respuesta validada contra el contrato por el cliente."""
    a = admin
    # --- 200/201 ---
    a.get("/api/admin/overview")
    a.get("/api/admin/settings")
    a.put("/api/admin/settings", json={"server_name": "Trajet de casa",
                                       "lan_url": "http://192.0.2.10:7796", "tailscale_url": None})
    dev = _pair_device(a)["device"]
    a.get("/api/admin/devices")
    a.patch(f"/api/admin/devices/{dev['id']}", json={"name": "iPhone 16 de Isma"})
    s = a.post("/api/admin/pairing").json()
    a.get(f"/api/admin/pairing/{s['id']}")
    a.delete(f"/api/admin/pairing/{s['id']}")
    a.get("/api/admin/prim-key")
    assert a.post("/api/admin/prim-key", json={"key": NUEVA}).status_code == 200
    a.post("/api/admin/prim-key/check")
    a.get("/api/admin/quota")
    a.get("/api/admin/errors")
    a.get("/api/admin/errors?limit=5")
    a.delete("/api/admin/prim-key", headers={"X-Trajet-Confirm": "borrar"})
    a.delete(f"/api/admin/devices/{dev['id']}")
    # --- 400 ---
    _err(a.patch(f"/api/admin/devices/{dev['id']}", json={"name": ""}), 400, "bad_request")
    _err(a.post("/api/admin/prim-key", json={"key": "corta"}), 400, "bad_request")
    _err(a.delete("/api/admin/prim-key"), 400, "bad_request", "X-Trajet-Confirm")
    _err(a.put("/api/admin/settings", json={"server_name": "x", "lan_url": "ftp://x",
                                            "tailscale_url": None}), 400, "bad_request", "lan_url")
    # --- 404 ---
    _err(a.get("/api/admin/pairing/no-existe"), 404, "not_found")
    _err(a.delete("/api/admin/pairing/no-existe"), 404, "not_found")
    _err(a.patch("/api/admin/devices/999", json={"name": "x"}), 404, "not_found")
    _err(a.delete("/api/admin/devices/999"), 404, "not_found")
    # --- 422: PRIM la rechaza ---
    fake_prim.fail["*"] = 401
    assert a.post("/api/admin/prim-key", json={"key": MALA}).status_code == 422
    fake_prim.fail.clear()
    # --- 403: sin la cabecera en lo que modifica; de otra IP en lo que lee ---
    for tpl, method, _ in admin_operations():
        url = tpl.replace("{pairing_id}", s["id"]).replace("{device_id}", str(dev["id"]))
        if method.upper() in UNSAFE:
            _err(a.request(method, url, panel=False, json={}), 403, "forbidden", "X-Trajet-Panel")
    monkeypatch.setattr(auth.settings, "admin_peers", "auto")
    for tpl, method, _ in admin_operations():
        url = tpl.replace("{pairing_id}", s["id"]).replace("{device_id}", str(dev["id"]))
        if method.upper() not in UNSAFE:
            _err(a.request(method, url, headers={"x-test-peer": "192.168.1.40"}), 403, "forbidden",
                 "TRAJET_ADMIN_PEERS")

    declarados = {(m, tpl, st) for tpl, m, op in admin_operations() for st in op["responses"]}
    assert declarados - a.seen == NO_PRODUCIDOS
    assert a.seen <= declarados


# =====================================================================
#  Clave PRIM
# =====================================================================

def test_guardar_clave_aplicada_en_caliente(admin, fake_prim):
    """200 saved: el tablero la usa en la siguiente peticion, sin reiniciar,
    y el contador de cuota empieza de cero para la clave nueva (R84)."""
    _con_ruta(admin, fake_prim)
    assert admin.client.get("/api/board").json()["legs"][0]["departures"]
    assert set(fake_prim.seen_keys) == {FAKE_KEY}
    hoy = admin.get("/api/admin/quota").json()["today"]["endpoints"]
    assert {e["endpoint"]: e["used"] for e in hoy}["stop-monitoring"] == 1

    n = len(fake_prim.seen_keys)
    r = admin.post("/api/admin/prim-key", json={"key": f"  {NUEVA}\n"})   # pegada con espacios
    assert r.status_code == 200
    res = r.json()
    assert res["saved"] is True and "error" not in res
    assert [c["api"] for c in res["checks"]] == ["stop-monitoring", "general-message", "navitia"]
    assert all(c["ok"] and c["status"] == 200 for c in res["checks"])
    info = res["info"]
    assert info["source"] == "panel" and info["last4"] == NUEVA[-4:]
    assert info["state"] == "valid" and info["encryption"] == "app_seed" and info["env_available"]
    # Las 3 comprobaciones van con la clave nueva
    assert fake_prim.seen_keys[n:] == [NUEVA] * 3

    hoy = admin.get("/api/admin/quota").json()["today"]["endpoints"]
    assert all(e["used"] == 0 for e in hoy)                   # cuota de la clave nueva
    n = len(fake_prim.seen_keys)
    assert admin.client.get("/api/board").json()["legs"][0]["departures"]    # sin esperar al TTL
    assert fake_prim.seen_keys[n:] and set(fake_prim.seen_keys[n:]) == {NUEVA}
    hoy = admin.get("/api/admin/quota").json()["today"]["endpoints"]
    assert {e["endpoint"]: e["used"] for e in hoy}["stop-monitoring"] == 1
    assert admin.get("/api/admin/prim-key").json()["source"] == "panel"
    with open(_secretos(), "rb") as f:
        assert NUEVA.encode() not in f.read()


@pytest.mark.parametrize("fallo, codigo, estado, texto", [
    (401, "prim_key_rejected", 401, "no válida"),
    (403, "prim_key_rejected", 403, "permiso"),
    ("timeout", "prim_unreachable", None, "no responde"),
    (429, "prim_quota_exhausted", 429, "cuota"),
])
def test_clave_rechazada_no_se_guarda(admin, fake_prim, fallo, codigo, estado, texto):
    """422 con el motivo por API y NADA guardado: la de antes sigue en uso."""
    _con_ruta(admin, fake_prim)
    fake_prim.fail["*"] = fallo
    r = admin.post("/api/admin/prim-key", json={"key": MALA})
    assert r.status_code == 422
    res = r.json()
    assert res["saved"] is False and res["error"]["code"] == codigo
    assert len(res["checks"]) == 3
    assert all(not c["ok"] and c["status"] == estado and texto in c["message"] for c in res["checks"])
    assert res["info"]["source"] == "env" and res["info"]["last4"] == FAKE_KEY[-4:]
    assert not os.path.exists(_secretos())

    fake_prim.fail.clear()
    n = len(fake_prim.seen_keys)
    admin.client.get("/api/board")
    assert set(fake_prim.seen_keys[n:]) == {FAKE_KEY}
    assert admin.get("/api/admin/prim-key").json()["source"] == "env"


def test_clave_sin_permiso_en_una_sola_api(admin, fake_prim):
    fake_prim.fail["navitia"] = 403
    res = admin.post("/api/admin/prim-key", json={"key": NUEVA}).json()
    assert res["saved"] is False and res["error"]["code"] == "prim_key_rejected"
    assert "navitia" in res["error"]["message"]
    por_api = {c["api"]: c for c in res["checks"]}
    assert por_api["stop-monitoring"]["ok"] and por_api["general-message"]["ok"]
    assert not por_api["navitia"]["ok"] and por_api["navitia"]["status"] == 403


@pytest.mark.parametrize("cuerpo, texto", [
    ({"key": "corta"}, "de 8 a 256"),
    ({"key": "x" * 257}, "de 8 a 256"),
    ({"key": "   "}, "de 8 a 256"),
    ({"key": "PANEL0clave con0espacios0dentro"}, "sin espacios"),
    ({"key": "PANEL0clave\n0partida0en0dos0"}, "sin espacios"),
    ({"key": 12345678}, "no es texto"),
    ({}, "falta 'key'"),
    ({"key": NUEVA, "otra": 1}, "otra"),
])
def test_clave_cuerpo_malo_400_sin_llamar_a_prim(admin, fake_prim, cuerpo, texto):
    body = _err(admin.post("/api/admin/prim-key", json=cuerpo), 400, "bad_request", texto)
    assert fake_prim.total_calls() == 0
    valor = cuerpo.get("key")
    if isinstance(valor, str) and len(valor.strip()) >= 6:
        assert valor.strip() not in json.dumps(body)            # sin eco


def test_clave_cuerpo_no_json(admin, fake_prim):
    for raw in (b"", b"no es json", b"[1, 2]", b"x" * (A._MAX_BODY + 1)):
        _err(admin.post("/api/admin/prim-key", content=raw,
                        headers={"Content-Type": "application/json"}), 400, "bad_request")
    assert fake_prim.total_calls() == 0


def test_reemplazo_y_borrado_con_prioridad_panel_sobre_entorno(admin, fake_prim):
    """Guardar, reemplazar y borrar. `source` dice cual manda: la del panel
    sobre la del entorno (R84); al borrarla vuelve la del entorno."""
    _con_ruta(admin, fake_prim)
    assert admin.get("/api/admin/prim-key").json()["source"] == "env"

    r1 = admin.post("/api/admin/prim-key", json={"key": NUEVA}).json()
    assert r1["info"]["source"] == "panel" and r1["info"]["last4"] == NUEVA[-4:]
    r2 = admin.post("/api/admin/prim-key", json={"key": OTRA}).json()
    assert r2["saved"] and r2["info"]["source"] == "panel" and r2["info"]["last4"] == OTRA[-4:]
    assert r2["info"]["saved_at"] >= r1["info"]["saved_at"]
    n = len(fake_prim.seen_keys)
    admin.client.get("/api/board")
    assert set(fake_prim.seen_keys[n:]) == {OTRA}

    # Borrar exige la confirmacion, y sin ella no se toca nada
    _err(admin.delete("/api/admin/prim-key"), 400, "bad_request", "X-Trajet-Confirm")
    _err(admin.delete("/api/admin/prim-key", headers={"X-Trajet-Confirm": "si"}), 400, "bad_request")
    _err(admin.delete("/api/admin/prim-key", headers={"X-Trajet-Confirm": "borrar"}, panel=False),
         403, "forbidden")
    assert admin.get("/api/admin/prim-key").json()["last4"] == OTRA[-4:]
    assert os.path.exists(_secretos())

    info = admin.delete("/api/admin/prim-key", headers={"X-Trajet-Confirm": "borrar"}).json()
    assert info["source"] == "env" and info["last4"] == FAKE_KEY[-4:] and info["saved_at"] is None
    assert info["encryption"] == "none"
    assert not os.path.exists(_secretos())
    n = len(fake_prim.seen_keys)
    admin.client.get("/api/board")
    assert set(fake_prim.seen_keys[n:]) == {FAKE_KEY}


def test_sin_clave_de_entorno(arrancar, fake_prim):
    """Sin PRIM_API_KEY: aviso de error arriba; se guarda en el panel y al
    borrarla el servidor se queda sin clave."""
    a = arrancar(PRIM_API_KEY="")
    ov = a.get("/api/admin/overview").json()
    assert ov["prim_key"]["configured"] is False and ov["prim_key"]["source"] == "none"
    assert ov["prim_key"]["state"] == "missing" and ov["prim_key"]["last4"] is None
    assert any(w["level"] == "error" and "No hay clave de PRIM" in w["text"] for w in ov["warnings"])
    # Recomprobar sin clave no llama a PRIM
    res = a.post("/api/admin/prim-key/check").json()
    assert res["error"]["code"] == "prim_key_missing" and fake_prim.total_calls() == 0

    assert a.post("/api/admin/prim-key", json={"key": NUEVA}).json()["info"]["source"] == "panel"
    ov = a.get("/api/admin/overview").json()
    assert not any("No hay clave" in w["text"] for w in ov["warnings"])

    info = a.delete("/api/admin/prim-key", headers={"X-Trajet-Confirm": "borrar"}).json()
    assert info == {**info, "configured": False, "source": "none", "last4": None,
                    "state": "missing", "env_available": False}


def test_recomprobar_la_clave_en_uso(admin, fake_prim):
    res = admin.post("/api/admin/prim-key/check").json()
    assert res["saved"] is False and "error" not in res and res["info"]["state"] == "valid"
    fake_prim.fail["general-message"] = 403
    res = admin.post("/api/admin/prim-key/check").json()
    assert res["error"]["code"] == "prim_key_rejected" and res["info"]["state"] == "forbidden"
    ov = admin.get("/api/admin/overview").json()
    assert any(w["level"] == "error" and "permiso" in w["text"] for w in ov["warnings"])


def test_la_clave_nunca_sale_del_servidor(admin, fake_prim, caplog):
    """R83: ni la clave del entorno, ni la del panel, ni una que PRIM rechaza
    salen en ninguna respuesta de /api/admin, /api/v1, /api/health, el HTML
    del panel, sus estaticos, los logs, error_log o la BD. Solo `last4`."""
    caplog.set_level(logging.DEBUG)
    _con_ruta(admin, fake_prim)
    token = _pair_device(admin)["token"]
    bearer = {"Authorization": f"Bearer {token}"}
    textos: list[str] = []

    def guardar(r):
        textos.append(r.text)
        return r

    guardar(admin.post("/api/admin/prim-key", json={"key": NUEVA}))
    fake_prim.fail["*"] = 401
    guardar(admin.post("/api/admin/prim-key", json={"key": MALA}))       # rechazada
    guardar(admin.post("/api/admin/prim-key/check"))                    # la en uso, con 401
    fake_prim.fail = {"navitia": 403}
    guardar(admin.client.get("/api/board"))
    guardar(admin.client.get("/api/v1/board", headers=bearer))
    fake_prim.fail.clear()
    guardar(admin.post("/api/admin/prim-key", json={"key": OTRA}))
    info = guardar(admin.get("/api/admin/prim-key")).json()
    assert info["last4"] == OTRA[-4:]                       # last4 si, en el panel
    for url in ("/api/admin/overview", "/api/admin/prim-key", "/api/admin/quota",
                "/api/admin/errors?limit=200", "/api/admin/settings", "/api/admin/devices"):
        guardar(admin.get(url))
    for url in ("/api/health", "/api/v1/ping", "/api/v1/health", "/api/v1/board",
                "/api/v1/devices/me", "/api/board", "/"):
        guardar(admin.client.get(url, headers=bearer))
    html = textos[-1]
    for ref in re.findall(r'(?:src|href)="(/panel/static/[^"#]+)', html):
        guardar(admin.client.get(ref))
    guardar(admin.delete("/api/admin/prim-key", headers={"X-Trajet-Confirm": "borrar"}))
    guardar(admin.get("/api/admin/errors?limit=200"))

    from app.config import settings
    con = sqlite3.connect(settings.db_path)
    try:
        textos.append("\n".join(con.iterdump()))
    finally:
        con.close()
    textos.append(caplog.text)
    todo = "\n".join(textos)
    for clave in (NUEVA, OTRA, MALA, FAKE_KEY):
        trozos = {clave[i:i + 8] for i in range(len(clave) - 7)}
        fugas = [t for t in trozos if t in todo]
        assert not fugas, "un trozo de una clave aparece en respuestas, logs o BD"


# =====================================================================
#  Cabecera, origen y de donde viene la conexion
# =====================================================================

def test_sin_cabecera_del_panel_403_y_sin_efecto(admin, fake_prim):
    dev = _pair_device(admin)["device"]
    s = admin.post("/api/admin/pairing").json()
    casos = [("post", "/api/admin/pairing", None),
             ("delete", f"/api/admin/pairing/{s['id']}", None),
             ("patch", f"/api/admin/devices/{dev['id']}", {"name": "otro"}),
             ("delete", f"/api/admin/devices/{dev['id']}", None),
             ("post", "/api/admin/prim-key", {"key": NUEVA}),
             ("delete", "/api/admin/prim-key", None),
             ("post", "/api/admin/prim-key/check", None),
             ("put", "/api/admin/settings", {"server_name": "x", "lan_url": None, "tailscale_url": None})]
    for method, url, body in casos:
        for h in ({}, {"X-Trajet-Panel": "0"}, {"X-Trajet-Panel": "true"}):
            _err(admin.request(method, url, panel=False, json=body,
                               headers={**h, "X-Trajet-Confirm": "borrar"}), 403, "forbidden",
                 "X-Trajet-Panel")
    assert fake_prim.total_calls() == 0
    assert admin.get(f"/api/admin/pairing/{s['id']}").json()["status"] == "pending"
    assert admin.get("/api/admin/devices").json()["devices"][0]["name"] == "iPhone de Isma"
    assert admin.get("/api/admin/prim-key").json()["source"] == "env"


def test_origen_distinto_403(admin, fake_prim):
    for origin in ("http://evil.example", "null", "http://testserver.evil.example"):
        _err(admin.post("/api/admin/prim-key", json={"key": NUEVA}, headers={"Origin": origin}),
             403, "forbidden", "Origin")
        _err(admin.put("/api/admin/settings", headers={"Origin": origin},
                       json={"server_name": "x", "lan_url": None, "tailscale_url": None}),
             403, "forbidden")
    assert fake_prim.total_calls() == 0
    r = admin.post("/api/admin/prim-key", json={"key": NUEVA}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_solo_desde_el_proxy_de_umbrel(admin, monkeypatch):
    """TRAJET_ADMIN_PEERS=auto: 127.0.0.1 o el proxy entran; otra app de la
    red Docker o alguien de la LAN que llegue directo, no."""
    monkeypatch.setattr(auth.settings, "admin_peers", "auto")
    monkeypatch.setattr(auth, "_default_gateway", lambda: "10.21.0.1")
    auth.reset_state()
    for peer in ("127.0.0.1", "10.21.0.1"):
        assert admin.get("/api/admin/overview", headers={"x-test-peer": peer}).status_code == 200
    for peer in ("10.21.0.7", "192.168.1.40"):
        _err(admin.get("/api/admin/prim-key", headers={"x-test-peer": peer}), 403, "forbidden")
        _err(admin.get("/api/admin/overview", headers={"x-test-peer": peer,
                                                       "X-Forwarded-For": "127.0.0.1"}),
             403, "forbidden")


# =====================================================================
#  Emparejamiento y dispositivos (R86, R87)
# =====================================================================

def test_flujo_de_emparejamiento_panel_app(admin):
    """Panel -> QR -> la app canjea el codigo -> el panel ve «emparejado»."""
    r = admin.post("/api/admin/pairing")
    assert r.status_code == 201
    s = r.json()
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", s["code"])
    assert s["ttl_s"] == 300 and s["qr_payload"].startswith("trajet://pair?v=1&code=")
    assert s["qr_svg"].startswith("<svg") and "<script" not in s["qr_svg"].lower()
    st = admin.get(f"/api/admin/pairing/{s['id']}").json()
    assert st["status"] == "pending" and "device" not in st

    r = admin.client.post("/api/v1/pair", json={"code": s["code"].lower().replace("-", " "),
                                                "device_name": "iPhone de Isma",
                                                "device_model": "iPhone17,1", "app_version": "2.0"})
    assert r.status_code == 200
    token = r.json()["token"]

    st = admin.get(f"/api/admin/pairing/{s['id']}").json()
    assert st["status"] == "used"
    assert st["device"]["name"] == "iPhone de Isma" and st["device"]["model"] == "iPhone17,1"
    devs = admin.get("/api/admin/devices").json()["devices"]
    assert [d["name"] for d in devs] == ["iPhone de Isma"] and "last_ip" in devs[0]
    assert admin.get("/api/admin/overview").json()["devices"] == 1
    # El codigo es de un solo uso
    r = admin.client.post("/api/v1/pair", json={"code": s["code"], "device_name": "otro"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "pairing_invalid"
    assert admin.client.get("/api/v1/devices/me",
                            headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_qr_anulado_caducado_y_el_nuevo_anula_el_anterior(admin, monkeypatch):
    s1 = admin.post("/api/admin/pairing").json()
    s2 = admin.post("/api/admin/pairing").json()
    assert s1["code"] != s2["code"]
    assert admin.get(f"/api/admin/pairing/{s1['id']}").json()["status"] == "cancelled"

    st = admin.delete(f"/api/admin/pairing/{s2['id']}").json()
    assert st["status"] == "cancelled"
    r = admin.client.post("/api/v1/pair", json={"code": s2["code"], "device_name": "x"})
    assert r.json()["error"]["code"] == "pairing_invalid"
    # Anular algo ya anulado no cambia nada
    assert admin.delete(f"/api/admin/pairing/{s2['id']}").json()["status"] == "cancelled"

    s3 = admin.post("/api/admin/pairing").json()
    real = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: real + timedelta(seconds=301))
    assert admin.get(f"/api/admin/pairing/{s3['id']}").json()["status"] == "expired"
    _err(admin.get("/api/admin/pairing/" + "x" * 65), 404, "not_found")


def test_revocar_desde_el_panel(admin):
    """R87: el token deja de valer en la siguiente peticion."""
    res = _pair_device(admin)
    bearer = {"Authorization": f"Bearer {res['token']}"}
    assert admin.client.get("/api/v1/devices/me", headers=bearer).status_code == 200
    r = admin.delete(f"/api/admin/devices/{res['device']['id']}")
    assert r.json() == {"revoked": res["device"]["id"]}
    r = admin.client.get("/api/v1/devices/me", headers=bearer)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert admin.get("/api/admin/devices").json()["devices"] == []
    assert admin.get("/api/admin/overview").json()["devices"] == 0
    _err(admin.delete(f"/api/admin/devices/{res['device']['id']}"), 404, "not_found")


def test_renombrar_dispositivo(admin):
    dev = _pair_device(admin)["device"]
    url = f"/api/admin/devices/{dev['id']}"
    r = admin.patch(url, json={"name": "  iPhone\nde   Isma  "})
    assert r.json()["name"] == "iPhone de Isma"
    for body, texto in (({"name": ""}, "de 1 a 60"), ({"name": "   "}, "de 1 a 60"),
                        ({"name": "x" * 61}, "de 1 a 60"), ({"name": 5}, "de 1 a 60"),
                        ({}, "falta 'name'"), ({"name": "a", "model": "b"}, "model")):
        _err(admin.patch(url, json=body), 400, "bad_request", texto)
    for otro in ("999", "abc", "1.5", "-1", "²"):
        _err(admin.patch(f"/api/admin/devices/{otro}", json={"name": "x"}), 404, "not_found")
        _err(admin.delete(f"/api/admin/devices/{otro}"), 404, "not_found")
    assert admin.get("/api/admin/devices").json()["devices"][0]["name"] == "iPhone de Isma"


# =====================================================================
#  Ajustes: nombre y direcciones del QR
# =====================================================================

def test_ajustes_se_reflejan_en_el_qr(admin):
    ajustes = {"server_name": "  Trajet   de casa ", "lan_url": "HTTP://192.0.2.10:7796/",
               "tailscale_url": "https://NAS.tail1234.ts.net"}
    r = admin.put("/api/admin/settings", json=ajustes)
    esperado = {"server_name": "Trajet de casa", "lan_url": "http://192.0.2.10:7796",
                "tailscale_url": "https://nas.tail1234.ts.net"}
    assert r.json() == esperado
    assert admin.get("/api/admin/settings").json() == esperado
    s = admin.post("/api/admin/pairing").json()
    assert s["qr_payload"] == (f"trajet://pair?v=1&code={s['code']}"
                               "&lan=http%3A%2F%2F192.0.2.10%3A7796"
                               "&ts=https%3A%2F%2Fnas.tail1234.ts.net&name=Trajet%20de%20casa")
    assert s["urls"] == [{"kind": "lan", "url": "http://192.0.2.10:7796"},
                         {"kind": "tailscale", "url": "https://nas.tail1234.ts.net"}]
    # Sin direcciones: el QR va sin ellas y el resumen lo avisa
    admin.put("/api/admin/settings", json={"server_name": "Trajet", "lan_url": None,
                                           "tailscale_url": ""})
    s = admin.post("/api/admin/pairing").json()
    assert "lan=" not in s["qr_payload"] and "ts=" not in s["qr_payload"] and s["urls"] == []
    ov = admin.get("/api/admin/overview").json()
    assert any(w["level"] == "warn" and "no lleva ninguna dirección" in w["text"]
               for w in ov["warnings"])
    # IPv6 entre corchetes
    r = admin.put("/api/admin/settings", json={"server_name": "Trajet",
                                               "lan_url": "http://[FD00::1]:7796",
                                               "tailscale_url": None})
    assert r.json()["lan_url"] == "http://[fd00::1]:7796"
    assert not any("dirección" in w["text"] for w in admin.get("/api/admin/overview").json()["warnings"])


@pytest.mark.parametrize("campo, valor", [
    ("lan_url", "ftp://192.0.2.10"),
    ("lan_url", "192.0.2.10:7796"),
    ("lan_url", "http://192.0.2.10:7796/api"),
    ("lan_url", "http://192.0.2.10:7796/?x=1"),
    ("lan_url", "http://192.0.2.10#x"),
    ("lan_url", "http://yo:secreto@192.0.2.10:7796"),
    ("lan_url", "http://192.0.2.10:99999"),
    ("lan_url", "http://192.0.2.10:0"),
    ("lan_url", "http://192.0.2.10:abc"),
    ("tailscale_url", "http://mal host:7796"),
    ("tailscale_url", "http://"),
    ("tailscale_url", "http://-x-.example"),
    ("tailscale_url", 7796),
    ("tailscale_url", "http://" + "a" * 200 + ".example"),
])
def test_ajustes_direccion_no_valida_400(admin, campo, valor):
    body = {"server_name": "Trajet", "lan_url": None, "tailscale_url": None, campo: valor}
    _err(admin.put("/api/admin/settings", json=body), 400, "bad_request", campo)
    assert admin.get("/api/admin/settings").json()[campo] is None


@pytest.mark.parametrize("body, texto", [
    ({"server_name": "x" * 41, "lan_url": None, "tailscale_url": None}, "40"),
    ({"server_name": None, "lan_url": None, "tailscale_url": None}, "server_name"),
    ({"lan_url": None, "tailscale_url": None}, "server_name"),
    ({"server_name": "x", "lan_url": None, "tailscale_url": None, "otro": 1}, "otro"),
])
def test_ajustes_cuerpo_malo_400(admin, body, texto):
    _err(admin.put("/api/admin/settings", json=body), 400, "bad_request", texto)


def test_ajustes_con_direcciones_del_entorno(arrancar):
    """Lo del panel manda; vaciarla la quita aunque este en el entorno; poner
    la misma que el entorno vuelve a seguir al entorno (sin fila)."""
    a = arrancar(TRAJET_LAN_URL="http://192.0.2.20:7796", TRAJET_SERVER_NAME="NAS de casa")
    assert a.get("/api/admin/settings").json() == {
        "server_name": "NAS de casa", "lan_url": "http://192.0.2.20:7796", "tailscale_url": None}
    a.put("/api/admin/settings", json={"server_name": "NAS de casa", "lan_url": None,
                                       "tailscale_url": None})
    assert a.get("/api/admin/settings").json()["lan_url"] is None
    assert db.get_setting("lan_url") == "" and db.get_setting("server_name") is None
    a.put("/api/admin/settings", json={"server_name": "", "lan_url": "http://192.0.2.20:7796/",
                                       "tailscale_url": None})
    assert db.get_setting("lan_url") is None
    assert a.get("/api/admin/settings").json() == {
        "server_name": "NAS de casa", "lan_url": "http://192.0.2.20:7796", "tailscale_url": None}


# =====================================================================
#  Cuota, errores y resumen
# =====================================================================

def test_cuota_hoy_e_historial_de_7_dias(admin, fake_prim):
    _con_ruta(admin, fake_prim)
    admin.client.get("/api/board")
    q = admin.get("/api/admin/quota").json()
    assert {e["endpoint"]: e["used"] for e in q["today"]["endpoints"]}["stop-monitoring"] == 1
    assert len(q["history"]) == 21
    dias = sorted({h["day_utc"] for h in q["history"]})
    assert len(dias) == 7 and dias[-1] == q["today"]["day_utc"]
    hoy = [h for h in q["history"] if h["day_utc"] == q["today"]["day_utc"]]
    assert {h["endpoint"]: h["used"] for h in hoy}["stop-monitoring"] == 1


def test_errores_recientes_sin_secretos(admin):
    lg = logging.getLogger("trajet.pruebas")
    lg.warning("aviso uno con apikey=%s", NUEVA)
    lg.error("fallo dos con Bearer trj_%s", "A" * 43)
    errs = admin.get("/api/admin/errors").json()["errors"]
    assert errs[0]["message"].startswith("fallo dos") and errs[0]["level"] == "ERROR"
    assert errs[1]["message"].startswith("aviso uno") and errs[1]["logger"] == "trajet.pruebas"
    todo = json.dumps(errs)
    assert NUEVA not in todo and "A" * 43 not in todo
    assert len(admin.get("/api/admin/errors?limit=1").json()["errors"]) == 1
    for raro in ("0", "-5", "abc", "999999", ""):
        n = len(admin.get(f"/api/admin/errors?limit={raro}").json()["errors"])
        assert 1 <= n <= 200


def test_resumen_completo(admin, fake_prim):
    from app.config import VERSION
    from app.migrations import LATEST
    _con_ruta(admin, fake_prim)
    admin.client.get("/api/board")
    _pair_device(admin)
    ov = admin.get("/api/admin/overview").json()
    assert ov["version"] == VERSION and ov["schema_version"] == LATEST and ov["uptime_s"] >= 0
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ov["now_paris"])
    assert ov["devices"] == 1 and ov["prim_key"]["source"] == "env"
    assert ov["database"]["routes"] == 1 and ov["database"]["size_bytes"] > 0
    assert ov["quota"]["level"] == "ok"
    assert ov["translator"] == {"ok": False, "reason": "sin configurar"}
    assert ov["collector"]["enabled"] is False
    # Sin direcciones del QR (el entorno de pruebas no tiene): aviso
    textos = [w["text"] for w in ov["warnings"]]
    assert any("no lleva ninguna dirección" in t for t in textos)
    assert not any("traducción" in t for t in textos)            # sin configurar no es un fallo


def test_resumen_avisos_de_cuota_ollama_mapa_y_cifrado(arrancar, fake_prim, monkeypatch):
    a = arrancar(TRAJET_SECRET_SEED="", TRAJET_LAN_URL="http://192.0.2.10:7796")
    ov = a.get("/api/admin/overview").json()
    assert ov["warnings"] == [{"level": "info", "text": ov["warnings"][0]["text"]}]
    assert "APP_SEED" in ov["warnings"][0]["text"]

    # Guardada sin APP_SEED: clave maestra local (y la cuota empieza de cero).
    assert a.post("/api/admin/prim-key", json={"key": NUEVA}).json()["info"]["encryption"] == \
        "local_master_key"
    counter = prim.get_client().quota_counter
    counter.observe("stop-monitoring", 250)        # 75 % -> warn
    counter.observe("general-message", 100)        # 90 % -> critical
    counter.observe("navitia", 0)                  # agotada

    async def ollama_caido():
        return {"ok": False, "reason": "no responde: conexión rechazada", "models": []}

    monkeypatch.setattr(T, "available_cached", ollama_caido)
    monkeypatch.setattr(mapdata, "status", lambda: {"cached_items": 3, "last_refresh": None,
                                                    "last_error": "el portal de IDFM no responde"})
    ov = a.get("/api/admin/overview").json()
    niveles = [w["level"] for w in ov["warnings"]]
    assert niveles == sorted(niveles, key=["error", "warn", "info"].index)   # graves primero
    por_texto = {w["text"]: w["level"] for w in ov["warnings"]}

    def nivel(trozo):
        hallados = [lv for t, lv in por_texto.items() if trozo in t]
        assert hallados, f"falta el aviso «{trozo}»: {list(por_texto)}"
        return hallados[0]

    assert nivel("Cuota de PRIM agotada (navitia al 100 %)") == "error"
    assert nivel("casi agotada (general-message al 90 %)") == "warn"
    assert nivel("Cuota de PRIM justa (stop-monitoring al 75 %)") == "warn"
    assert nivel("clave maestra local") == "warn"
    assert nivel("traducción de avisos no funciona") == "info"
    assert nivel("el portal de IDFM no responde") == "info"
    assert ov["quota"]["level"] == "exhausted"


def test_resumen_con_clave_rechazada_y_prim_caido(admin, fake_prim):
    _con_ruta(admin, fake_prim)
    fake_prim.fail["*"] = 401
    admin.client.get("/api/board")
    ov = admin.get("/api/admin/overview").json()
    assert ov["prim_key"]["state"] == "invalid"
    assert any(w["level"] == "error" and "PRIM rechaza la clave" in w["text"] for w in ov["warnings"])
    fake_prim.fail["*"] = "timeout"
    admin.post("/api/admin/prim-key/check")
    ov = admin.get("/api/admin/overview").json()
    assert ov["prim_key"]["state"] == "unreachable"
    assert any(w["level"] == "warn" and "PRIM no responde" in w["text"] for w in ov["warnings"])


def test_resumen_no_se_cae_si_falla_una_pieza(admin, monkeypatch):
    def rota():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(P, "accuracy", rota)
    monkeypatch.setattr(auth, "active_device_count", rota)
    r = admin.get("/api/admin/overview")
    assert r.status_code == 200
    ov = r.json()
    assert ov["platform_model"]["predictions"] == 0 and ov["devices"] == 0
    textos = " ".join(w["text"] for w in ov["warnings"])
    assert "la previsión de vía" in textos and "los dispositivos" in textos


def test_memoria_del_contenedor(tmp_path, monkeypatch):
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t   81234 kB\nVmSwap:\t0 kB\n", encoding="ascii")
    v2 = tmp_path / "memory.max"
    v1 = tmp_path / "memory.limit_in_bytes"
    monkeypatch.setattr(A, "PROC_STATUS", str(status))
    monkeypatch.setattr(A, "CGROUP_LIMITS", (str(v2), str(v1)))
    assert A.memory_usage() == {"rss_bytes": 81234 * 1024, "limit_bytes": None}
    v2.write_text("402653184\n", encoding="ascii")                     # 384 MB (Umbrel)
    assert A.memory_usage()["limit_bytes"] == 402653184
    v2.write_text("max\n", encoding="ascii")                           # cgroup v2 sin limite
    v1.write_text("1000\n", encoding="ascii")
    assert A.memory_usage()["limit_bytes"] is None
    v2.unlink()
    v1.write_text("9223372036854771712\n", encoding="ascii")          # cgroup v1 sin limite
    assert A.memory_usage()["limit_bytes"] is None
    v1.write_text("536870912\n", encoding="ascii")
    assert A.memory_usage()["limit_bytes"] == 536870912
    monkeypatch.setattr(A, "PROC_STATUS", str(tmp_path / "no-existe"))
    assert A.memory_usage()["rss_bytes"] is None


def test_normalizar_direcciones():
    ok = {"http://192.0.2.1:7796": "http://192.0.2.1:7796",
          " https://Umbrel.Local/ ": "https://umbrel.local",
          "http://nas_casa:80": "http://nas_casa:80",
          "http://[::1]:7796//": "http://[::1]:7796"}
    for entrada, salida in ok.items():
        assert A.normalize_base_url(entrada, "x") == salida
        assert urlsplit(salida).path == ""
    assert A.normalize_base_url(None, "x") is None and A.normalize_base_url("  ", "x") is None
