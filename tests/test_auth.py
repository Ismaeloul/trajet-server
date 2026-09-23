"""Emparejamiento, tokens y acceso al panel (R85, R86, R87).

Todo sin red: el panel se simula llamando a auth.new_pairing (las rutas de
/api/admin son de otro modulo) y la IP de la conexion con PeerApp.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
from datetime import timedelta

import pytest
from _seg_contrato import (ContractClient, PeerApp, auth_limpio,  # noqa: F401
                           pair, sample_path, sample_query, schema_validator,
                           v1_operations)
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import auth, db
from app.api import errors

TOKEN_RE = re.compile(r"^trj_[A-Za-z0-9_-]{43}$")


@pytest.fixture
def v1(client):
    return ContractClient(client)


@pytest.fixture
def peer_client(app):
    """TestClient cuya IP de conexion se elige con la cabecera x-test-peer."""
    with TestClient(PeerApp(app)) as c:
        yield ContractClient(c)


def _pair_body(code: str, name: str = "iPhone de pruebas") -> dict:
    return {"code": code, "device_name": name, "device_model": "iPhone17,1", "app_version": "2.0"}


def _redeem(v1, code: str, peer: str | None = None):
    headers = {"x-test-peer": peer} if peer else {}
    return v1.post("/api/v1/pair", json=_pair_body(code), headers=headers)


def _later(monkeypatch, seconds: float):
    real = auth._now()
    monkeypatch.setattr(auth, "_now", lambda: real + timedelta(seconds=seconds))


# ---------------- el codigo y el QR ----------------

def test_pairing_session_forma_qr_y_solo_hash(v1, monkeypatch):
    monkeypatch.setattr(auth.settings, "lan_url", "http://192.0.2.10:7796")
    monkeypatch.setattr(auth.settings, "tailscale_url", "http://100.64.0.9:7796/")
    monkeypatch.setattr(auth.settings, "server_name", "Trajet de casa")
    s = auth.new_pairing()
    schema_validator("PairingSession").validate(s)
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", s["code"])
    assert s["ttl_s"] == 300
    assert s["qr_payload"] == (
        f"trajet://pair?v=1&code={s['code']}"
        "&lan=http%3A%2F%2F192.0.2.10%3A7796&ts=http%3A%2F%2F100.64.0.9%3A7796"
        "&name=Trajet%20de%20casa")
    assert s["urls"] == [{"kind": "lan", "url": "http://192.0.2.10:7796"},
                         {"kind": "tailscale", "url": "http://100.64.0.9:7796"}]
    assert s["qr_svg"].startswith("<svg") and "viewBox" in s["qr_svg"]
    # En la BD solo el SHA-256 del codigo normalizado, nunca el codigo.
    norm = s["code"].replace("-", "")
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM pairing_codes")]
    assert rows[0]["code_hash"] == hashlib.sha256(norm.encode()).hexdigest()
    dump = repr(rows)
    assert norm not in dump and s["code"] not in dump


def test_direcciones_del_panel_mandan_sobre_el_entorno(v1, monkeypatch):
    monkeypatch.setattr(auth.settings, "lan_url", "http://192.0.2.10:7796")
    monkeypatch.setattr(auth.settings, "tailscale_url", "http://100.64.0.9:7796")
    db.set_setting("lan_url", "http://192.0.2.77:7796")
    db.set_setting("tailscale_url", "")            # quitada a proposito en el panel
    db.set_setting("server_name", "NAS")
    s = auth.new_pairing()
    assert s["urls"] == [{"kind": "lan", "url": "http://192.0.2.77:7796"}]
    assert "&ts=" not in s["qr_payload"] and s["qr_payload"].endswith("&name=NAS")
    assert auth.server_info()["name"] == "NAS"


def test_sin_direcciones_el_qr_no_las_lleva(v1, monkeypatch):
    monkeypatch.setattr(auth.settings, "lan_url", "")
    monkeypatch.setattr(auth.settings, "tailscale_url", "")
    s = auth.new_pairing()
    assert s["urls"] == []
    assert "lan=" not in s["qr_payload"] and "ts=" not in s["qr_payload"]


def test_nuevo_codigo_anula_el_anterior(v1):
    """R86: solo vale el ultimo QR que se ha ensenado."""
    viejo = auth.new_pairing()
    nuevo = auth.new_pairing()
    assert auth.pairing_status(viejo["id"])["status"] == "cancelled"
    assert auth.pairing_status(nuevo["id"])["status"] == "pending"
    r = _redeem(v1, viejo["code"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "pairing_invalid"
    assert _redeem(v1, nuevo["code"]).status_code == 200


# ---------------- canje: un solo uso, 5 min, misma respuesta ----------------

def test_pair_ok_y_estado_used(v1):
    s = auth.new_pairing()
    r = _redeem(v1, s["code"])
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert TOKEN_RE.match(body["token"]) and len(body["token"]) == 47
    assert "last_ip" not in body["device"]
    st = auth.pairing_status(s["id"])
    schema_validator("PairingStatus").validate(st)
    assert st["status"] == "used" and st["device"]["id"] == body["device"]["id"]


@pytest.mark.parametrize("escrito", ["{c}", "{lower}", "{sin_guion}", " {spaced} "])
def test_pair_normaliza_el_codigo(v1, escrito):
    s = auth.new_pairing()
    c = s["code"]
    code = escrito.format(c=c, lower=c.lower(), sin_guion=c.replace("-", ""),
                          spaced=c.replace("-", " ").lower())
    assert _redeem(v1, code).status_code == 200


def test_pair_caducado(v1, monkeypatch):
    """R86: a los 5 min el codigo ya no vale."""
    s = auth.new_pairing()
    _later(monkeypatch, 301)
    r = _redeem(v1, s["code"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "pairing_invalid"
    assert auth.pairing_status(s["id"])["status"] == "expired"


def test_pair_justo_antes_de_caducar_vale(v1, monkeypatch):
    s = auth.new_pairing()
    _later(monkeypatch, 299)
    assert _redeem(v1, s["code"]).status_code == 200


def test_pair_reutilizado(v1):
    """R86: un solo uso."""
    s = auth.new_pairing()
    assert _redeem(v1, s["code"]).status_code == 200
    r = _redeem(v1, s["code"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "pairing_invalid"
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1


def test_pair_anulado(v1):
    s = auth.new_pairing()
    st = auth.cancel_pairing(s["id"])
    assert st["status"] == "cancelled"
    r = _redeem(v1, s["code"])
    assert r.status_code == 401 and r.json()["error"]["code"] == "pairing_invalid"
    # Anular algo ya anulado o que no existe
    assert auth.cancel_pairing(s["id"])["status"] == "cancelled"
    with pytest.raises(errors.ApiError) as e:
        auth.cancel_pairing("no-existe")
    assert e.value.code == "not_found"


def test_pair_misma_respuesta_para_malo_caducado_usado_anulado(peer_client, monkeypatch):
    """R86: no se da ninguna pista de por que no vale."""
    v1 = peer_client
    respuestas = []
    # mal escrito (alfabeto valido, codigo que no existe)
    auth.new_pairing()
    respuestas.append(_redeem(v1, "ABCD-EFGH", "10.0.0.1"))
    # con caracteres imposibles (0/O/1/I)
    respuestas.append(_redeem(v1, "OOII-0011", "10.0.0.2"))
    # usado
    s = auth.new_pairing()
    assert _redeem(v1, s["code"], "10.0.0.3").status_code == 200
    respuestas.append(_redeem(v1, s["code"], "10.0.0.3"))
    # anulado
    s = auth.new_pairing()
    auth.cancel_pairing(s["id"])
    respuestas.append(_redeem(v1, s["code"], "10.0.0.4"))
    # caducado
    s = auth.new_pairing()
    _later(monkeypatch, 600)
    respuestas.append(_redeem(v1, s["code"], "10.0.0.5"))

    assert {r.status_code for r in respuestas} == {401}
    assert len({r.content for r in respuestas}) == 1
    assert len({tuple(sorted((k, v) for k, v in r.headers.items()
                             if k not in ("date", "content-length"))) for r in respuestas}) == 1


@pytest.mark.parametrize("body", [
    {}, {"code": "ABCD-EFGH"}, {"device_name": "x"},
    {"code": "ABC", "device_name": "x"},                       # < 8
    {"code": "A" * 17, "device_name": "x"},                    # > 16
    {"code": "ABCD-EFGH", "device_name": ""},
    {"code": "ABCD-EFGH", "device_name": "x" * 61},
    {"code": "ABCD-EFGH", "device_name": "x", "device_model": "m" * 61},
    {"code": "ABCD-EFGH", "device_name": "x", "app_version": "9" * 31},
    {"code": 12345678, "device_name": "x"},
    ["no", "es", "un", "objeto"],
])
def test_pair_cuerpo_invalido_400(v1, body):
    r = v1.post("/api/v1/pair", json=body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


def test_pair_cuerpo_no_json_o_enorme_400(v1):
    r = v1.post("/api/v1/pair", content=b"{no es json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = v1.post("/api/v1/pair", content=b"{" + b" " * 10_000 + b"}",
                headers={"content-type": "application/json"})
    assert r.status_code == 400


# ---------------- fuerza bruta ----------------

def test_fuerza_bruta_por_ip_429_con_retry_after(peer_client):
    """R86: 5 intentos por minuto y por IP."""
    s = auth.new_pairing()
    for _ in range(5):
        assert _redeem(peer_client, "ABCD-EFGH", "10.1.1.1").status_code == 401
    r = _redeem(peer_client, s["code"], "10.1.1.1")     # ni con el bueno
    assert r.status_code == 429
    body = r.json()["error"]
    assert body["code"] == "rate_limited"
    assert 1 <= int(r.headers["retry-after"]) <= 60
    assert body["retry_after"] == int(r.headers["retry-after"])
    # Desde otra IP si se puede (y el bueno sigue valiendo: 5 fallos < 10)
    assert _redeem(peer_client, s["code"], "10.1.1.2").status_code == 200


def test_rate_limit_se_libera_con_el_tiempo(peer_client, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(auth, "_mono", lambda: t[0])
    for _ in range(5):
        _redeem(peer_client, "ABCD-EFGH", "10.1.1.1")
    t[0] += 30
    r = _redeem(peer_client, "ABCD-EFGH", "10.1.1.1")
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    t[0] += 30
    assert _redeem(peer_client, "ABCD-EFGH", "10.1.1.1").status_code == 401


def test_fuerza_bruta_global_20_cada_5_min(peer_client, monkeypatch):
    """R86: con IPs distintas manda el limite global."""
    t = [5000.0]
    monkeypatch.setattr(auth, "_mono", lambda: t[0])
    for i in range(20):
        # 20 intentos repartidos entre IPs; cada 9 se mete un canje bueno
        # para que no salte la anulacion por 10 fallos seguidos.
        ip = f"10.2.0.{i // 4}"
        if i % 9 == 8:
            s = auth.new_pairing()
            assert _redeem(peer_client, s["code"], ip).status_code == 200
        else:
            assert _redeem(peer_client, "ABCD-EFGH", ip).status_code == 401
    r = _redeem(peer_client, "ABCD-EFGH", "10.2.9.9")
    assert r.status_code == 429
    assert 1 <= int(r.headers["retry-after"]) <= 300
    t[0] += 300
    assert _redeem(peer_client, "ABCD-EFGH", "10.2.9.9").status_code == 401


def test_diez_fallos_seguidos_anulan_los_codigos_vivos(peer_client):
    """R86: tras 10 fallos seguidos hay que generar otro codigo en el panel."""
    s = auth.new_pairing()
    for i in range(10):
        assert _redeem(peer_client, "ABCD-EFGH", f"10.3.0.{i // 5}").status_code == 401
    r = _redeem(peer_client, s["code"], "10.3.0.9")
    assert r.status_code == 401 and r.json()["error"]["code"] == "pairing_invalid"
    assert auth.pairing_status(s["id"])["status"] == "cancelled"
    # Uno nuevo vale
    s2 = auth.new_pairing()
    assert _redeem(peer_client, s2["code"], "10.3.0.8").status_code == 200


def test_un_acierto_reinicia_la_cuenta_de_fallos(peer_client):
    s = auth.new_pairing()
    for i in range(9):
        _redeem(peer_client, "ABCD-EFGH", f"10.4.0.{i // 5}")
    assert _redeem(peer_client, s["code"], "10.4.0.5").status_code == 200
    s2 = auth.new_pairing()
    for i in range(9):
        _redeem(peer_client, "ABCD-EFGH", f"10.4.1.{i // 5}")
    assert auth.pairing_status(s2["id"])["status"] == "pending"


# ---------------- tokens ----------------

def _all_db_bytes(env_dir) -> bytes:
    out = b""
    for name in os.listdir(env_dir):
        if name.startswith("trajet.db"):
            with open(os.path.join(env_dir, name), "rb") as f:
                out += f.read()
    return out


def test_token_hash_y_compare_digest(v1, env, monkeypatch):
    """R85: 256 bits, en la BD solo su SHA-256, comparado en tiempo constante."""
    res = pair(v1)
    token = res["token"]
    assert TOKEN_RE.match(token)
    assert pair(v1, "otro")["token"] != token

    # Ni en las filas ni en los ficheros de SQLite (incluido el WAL).
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM devices")]
    assert rows[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(rows)
    raw = _all_db_bytes(env)
    assert token.encode() not in raw and token[4:].encode() not in raw

    usados = []
    real = hmac.compare_digest

    def espia(a, b):
        usados.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", espia)
    assert v1.with_token(token).get("/api/v1/devices/me").status_code == 200
    assert (rows[0]["token_hash"], rows[0]["token_hash"]) in usados


def test_token_revocado_401_al_momento(v1):
    """R87: el panel revoca y la siguiente peticion ya da 401."""
    res = pair(v1)
    cli = v1.with_token(res["token"])
    assert cli.get("/api/v1/devices/me").status_code == 200
    assert auth.revoke_device(res["device"]["id"]) is True
    r = cli.get("/api/v1/devices/me")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert cli.get("/api/v1/ping").json()["paired"] is False
    assert auth.revoke_device(res["device"]["id"]) is False       # ya estaba
    assert auth.get_device(res["device"]["id"]) is None
    assert auth.list_devices() == []


def test_desemparejar_desde_el_iphone(v1):
    """R87: DELETE /devices/me revoca su propio token."""
    res = pair(v1)
    cli = v1.with_token(res["token"])
    assert cli.delete("/api/v1/devices/me").json() == {"revoked": True}
    assert cli.get("/api/v1/devices/me").status_code == 401


def _v1_ops_con_token():
    return [(p, m, op) for p, m, op in v1_operations() if op.get("security") != []]


@pytest.mark.parametrize("path,method,op", _v1_ops_con_token(),
                         ids=[f"{m.upper()} {p}" for p, m, _ in _v1_ops_con_token()])
def test_v1_sin_token_401(client, path, method, op):
    """R85: TODAS las rutas de /api/v1 salvo ping y pair exigen token."""
    v1 = ContractClient(client)
    url = sample_path(path)
    kw = {"params": sample_query(op)}
    if method in ("post", "put"):
        kw["json"] = {"name": "x"}
    # Un token revocado
    res = pair(v1, "revocado")
    auth.revoke_device(res["device"]["id"])
    for headers in ({}, {"Authorization": "Bearer trj_" + "A" * 43},
                    {"Authorization": "Bearer " + "x" * 20},
                    {"Authorization": f"Basic {res['token']}"},
                    {"Authorization": f"Bearer {res['token']}"},
                    {"Authorization": "Bearer"}):
        r = v1.request(method, url, headers=headers, **kw)
        assert r.status_code == 401, (method, path, headers, r.text)
        assert r.json()["error"]["code"] == "unauthorized"
        assert r.headers["www-authenticate"].lower().startswith("bearer")


def test_ping_nunca_401_ni_escribe(v1):
    assert v1.get("/api/v1/ping").json()["paired"] is False
    r = v1.get("/api/v1/ping", headers={"Authorization": "Bearer trj_" + "B" * 43})
    assert r.status_code == 200 and r.json()["paired"] is False
    res = pair(v1)
    with db.conn() as c:
        c.execute("UPDATE devices SET last_used_at = '2020-01-01T00:00:00+00:00'")
    r = v1.with_token(res["token"]).get("/api/v1/ping")
    assert r.json()["paired"] is True
    with db.conn() as c:
        assert c.execute("SELECT last_used_at FROM devices").fetchone()[0] == "2020-01-01T00:00:00+00:00"


def test_last_used_como_mucho_una_vez_por_minuto(v1, monkeypatch):
    res = pair(v1)
    cli = v1.with_token(res["token"])
    with db.conn() as c:
        c.execute("UPDATE devices SET last_used_at = '2020-01-01T00:00:00+00:00', last_ip = NULL")
    cli.get("/api/v1/devices/me")
    with db.conn() as c:
        first = dict(c.execute("SELECT last_used_at, last_ip FROM devices").fetchone())
    assert first["last_used_at"] != "2020-01-01T00:00:00+00:00"
    assert first["last_ip"] == "testclient"

    _later(monkeypatch, 30)
    cli.get("/api/v1/devices/me")
    with db.conn() as c:
        assert c.execute("SELECT last_used_at FROM devices").fetchone()[0] == first["last_used_at"]

    _later(monkeypatch, 61)
    cli.get("/api/v1/devices/me")
    with db.conn() as c:
        assert c.execute("SELECT last_used_at FROM devices").fetchone()[0] > first["last_used_at"]


def test_dispositivos_del_panel(v1):
    a = pair(v1, "iPhone de Isma")["device"]
    b = pair(v1, "iPad")["device"]
    lista = auth.list_devices()
    assert [d["id"] for d in lista] == [a["id"], b["id"]]
    for d in lista:
        schema_validator("Device").validate(d)
        assert "last_ip" in d                      # el panel si la ve
    assert auth.active_device_count() == 2
    ren = auth.rename_device(a["id"], "  iPhone\tnuevo ")
    assert ren["name"] == "iPhone nuevo"
    for malo in ("", "   ", "x" * 61, None):
        with pytest.raises(errors.ApiError) as e:
            auth.rename_device(a["id"], malo)
        assert e.value.code == "bad_request"
    with pytest.raises(errors.ApiError) as e:
        auth.rename_device(999, "x")
    assert e.value.code == "not_found"
    auth.revoke_device(b["id"])
    assert auth.active_device_count() == 1
    with pytest.raises(errors.ApiError):
        auth.rename_device(b["id"], "revocado")


# ---------------- IP del cliente ----------------

@pytest.fixture
def route_file(tmp_path, monkeypatch):
    """Un /proc/net/route de mentira con la puerta de enlace 10.21.0.1."""
    f = tmp_path / "route"
    f.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t0000150A\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth0\t00000000\t0100150A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n", encoding="ascii")
    monkeypatch.setattr(auth, "PROC_ROUTE", str(f))
    monkeypatch.delenv("APP_PROXY_HOSTNAME", raising=False)
    auth.reset_state()
    return f


def test_puerta_de_enlace_de_proc_net_route(route_file):
    assert auth._default_gateway() == "10.21.0.1"


def test_client_ip_solo_se_fia_del_proxy(peer_client, route_file):
    # Por el proxy de Umbrel (puerta de enlace): cuenta X-Forwarded-For, el
    # ULTIMO valor (el que pone el proxy; el primero lo escribe el cliente).
    s = auth.new_pairing()
    r = peer_client.post("/api/v1/pair", json=_pair_body(s["code"]),
                         headers={"x-test-peer": "10.21.0.1",
                                  "X-Forwarded-For": "6.6.6.6, 192.168.1.40"})
    dev = auth.get_device(r.json()["device"]["id"])
    assert dev["last_ip"] == "192.168.1.40"
    # Directo desde otra app de la red Docker: X-Forwarded-For no vale nada.
    s = auth.new_pairing()
    r = peer_client.post("/api/v1/pair", json=_pair_body(s["code"]),
                         headers={"x-test-peer": "10.21.0.7", "X-Forwarded-For": "192.168.1.40"})
    assert auth.get_device(r.json()["device"]["id"])["last_ip"] == "10.21.0.7"


def test_xff_inventado_no_salta_el_limite_por_ip(peer_client, route_file):
    for i in range(5):
        h = {"x-test-peer": "172.20.0.5", "X-Forwarded-For": f"1.1.1.{i}"}
        r = peer_client.post("/api/v1/pair", json=_pair_body("ABCD-EFGH"), headers=h)
        assert r.status_code == 401
    r = peer_client.post("/api/v1/pair", json=_pair_body("ABCD-EFGH"),
                         headers={"x-test-peer": "172.20.0.5", "X-Forwarded-For": "1.1.1.99"})
    assert r.status_code == 429


# ---------------- panel: require_panel ----------------

def _panel_app():
    """App minima con una ruta de /api/admin protegida (las de verdad son de
    otro modulo): prueba require_panel sin depender de ellas."""
    app = FastAPI()
    errors.install(app)

    @app.get("/api/admin/prueba", dependencies=[Depends(auth.require_panel)])
    async def leer():
        return {"ok": True}

    @app.post("/api/admin/prueba", dependencies=[Depends(auth.require_panel)])
    async def cambiar():
        return {"ok": True}

    @app.delete("/api/admin/prueba", dependencies=[Depends(auth.require_panel)])
    async def borrar():
        return {"ok": True}

    return TestClient(PeerApp(app))


def _forbidden(r, texto: str | None = None):
    assert r.status_code == 403, r.text
    schema_validator("ErrorV1").validate(r.json())
    assert r.json()["error"]["code"] == "forbidden"
    if texto:
        assert texto in r.json()["error"]["message"]


def test_panel_auto_proxy_de_umbrel(env, route_file, monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_peers", "auto")
    c = _panel_app()
    ok = {"X-Trajet-Panel": "1"}
    assert c.get("/api/admin/prueba", headers={"x-test-peer": "10.21.0.1"}).status_code == 200
    assert c.get("/api/admin/prueba", headers={"x-test-peer": "127.0.0.1"}).status_code == 200
    assert c.get("/api/admin/prueba", headers={"x-test-peer": "::1"}).status_code == 200
    assert c.get("/api/admin/prueba", headers={"x-test-peer": "::ffff:10.21.0.1"}).status_code == 200
    assert c.post("/api/admin/prueba", headers={"x-test-peer": "10.21.0.1", **ok}).status_code == 200
    # Otra app de la red Docker, o alguien de la LAN que llegue directo
    for peer in ("10.21.0.7", "192.168.1.40", "testclient"):
        _forbidden(c.get("/api/admin/prueba", headers={"x-test-peer": peer}), "TRAJET_ADMIN_PEERS")
    # X-Forwarded-For no cuenta para esto: manda la conexion
    _forbidden(c.get("/api/admin/prueba", headers={"x-test-peer": "10.21.0.7",
                                                   "X-Forwarded-For": "10.21.0.1"}))


def test_panel_auto_con_app_proxy_hostname(env, route_file, monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_peers", "auto")
    monkeypatch.setenv("APP_PROXY_HOSTNAME", "trajet_app_proxy_1")
    monkeypatch.setattr(auth, "_resolve", lambda host: {"10.21.21.5"} if host == "trajet_app_proxy_1" else set())
    auth.reset_state()
    c = _panel_app()
    assert c.get("/api/admin/prueba", headers={"x-test-peer": "10.21.21.5"}).status_code == 200
    _forbidden(c.get("/api/admin/prueba", headers={"x-test-peer": "10.21.21.6"}))


def test_panel_auto_sin_proc_net_route(env, tmp_path, monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_peers", "auto")
    monkeypatch.setattr(auth, "PROC_ROUTE", str(tmp_path / "no-existe"))
    monkeypatch.delenv("APP_PROXY_HOSTNAME", raising=False)
    auth.reset_state()
    c = _panel_app()
    assert c.get("/api/admin/prueba", headers={"x-test-peer": "127.0.0.1"}).status_code == 200
    _forbidden(c.get("/api/admin/prueba", headers={"x-test-peer": "10.21.0.1"}))


def test_panel_any(env, monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_peers", "any")
    c = _panel_app()
    for peer in ("192.168.1.40", "testclient", "8.8.8.8"):
        assert c.get("/api/admin/prueba", headers={"x-test-peer": peer}).status_code == 200


def test_panel_redes_cidr(env, monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_peers", "192.168.1.0/24, 10.21.0.1 ,basura, fd00::/8")
    c = _panel_app()
    for peer in ("192.168.1.40", "10.21.0.1", "fd00::5"):
        assert c.get("/api/admin/prueba", headers={"x-test-peer": peer}).status_code == 200
    for peer in ("192.168.2.1", "10.21.0.2", "fe80::1", "testclient"):
        _forbidden(c.get("/api/admin/prueba", headers={"x-test-peer": peer}), "TRAJET_ADMIN_PEERS")


def test_panel_cabecera_y_origen(env, monkeypatch):
    monkeypatch.setattr(auth.settings, "admin_peers", "any")
    c = _panel_app()
    # GET no necesita la cabecera
    assert c.get("/api/admin/prueba").status_code == 200
    # Lo que modifica, si
    _forbidden(c.post("/api/admin/prueba"), "X-Trajet-Panel")
    _forbidden(c.delete("/api/admin/prueba", headers={"X-Trajet-Panel": "0"}), "X-Trajet-Panel")
    assert c.post("/api/admin/prueba", headers={"X-Trajet-Panel": "1"}).status_code == 200
    # Origin: el mismo servidor vale; otra web (o «null») no
    assert c.post("/api/admin/prueba", headers={"X-Trajet-Panel": "1",
                                                "Origin": "http://testserver"}).status_code == 200
    for origin in ("http://evil.example", "http://testserver.evil.example", "null",
                   "http://testserver:8080"):
        _forbidden(c.post("/api/admin/prueba", headers={"X-Trajet-Panel": "1", "Origin": origin}))


def test_panel_origen_detras_del_proxy(env, route_file, monkeypatch):
    """Si el proxy cambia Host, vale el X-Forwarded-Host que pone el proxy
    (y solo si la conexion viene del proxy)."""
    monkeypatch.setattr(auth.settings, "admin_peers", "any")
    c = _panel_app()
    h = {"X-Trajet-Panel": "1", "Origin": "http://umbrel.local:7796",
         "X-Forwarded-Host": "umbrel.local:7796"}
    assert c.post("/api/admin/prueba", headers={"x-test-peer": "10.21.0.1", **h}).status_code == 200
    _forbidden(c.post("/api/admin/prueba", headers={"x-test-peer": "192.168.1.40", **h}))


def test_tabla_pairing_y_devices_sin_secretos_en_claro(v1, env):
    """Repaso final: ni codigos ni tokens en claro en ninguna tabla."""
    s = auth.new_pairing()
    res = pair(v1)
    con = sqlite3.connect(os.path.join(env, "trajet.db"))
    try:
        dump = "\n".join(con.iterdump())
    finally:
        con.close()
    assert res["token"] not in dump
    assert s["code"] not in dump and s["code"].replace("-", "") not in dump


def test_aviso_de_fuerza_bruta_una_vez_por_minuto(peer_client, caplog, monkeypatch):
    """Insistir no llena error_log: un aviso por minuto como mucho."""
    t = [100.0]
    monkeypatch.setattr(auth, "_mono", lambda: t[0])
    for _ in range(12):
        _redeem(peer_client, "ABCD-EFGH", "10.5.0.1")
    avisos = [r for r in caplog.records if "demasiados intentos" in r.getMessage()]
    assert len(avisos) == 1
    t[0] += 61
    for _ in range(6):
        _redeem(peer_client, "ABCD-EFGH", "10.5.0.1")
    avisos = [r for r in caplog.records if "demasiados intentos" in r.getMessage()]
    assert len(avisos) == 2
