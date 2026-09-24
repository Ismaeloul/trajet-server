"""La clave PRIM por la API del panel, contra PRIM REAL, de principio a fin.

13 llamadas reales (5 stop-monitoring, 4 general-message, 4 navitia):
  1. pegar una clave falsa -> PRIM la rechaza (401) y no se guarda   (3)
  2. pegar la real -> se prueba, se cifra, se guarda y se usa          (3)
     ... y el servidor, que arranco SIN clave, ya la usa sin reiniciar (1)
  3. reemplazarla (volver a guardar la real) -> en caliente             (3)
  4. recomprobar la clave en uso                                        (3)
  5. borrarla -> se vuelve a «ninguna» (no hay clave de entorno)        (0)

Va con el conftest de tests/real: cada llamada pasa antes por el contador
de 800/endpoint/dia (.local/real-quota-<dia>.json). La clave real NUNCA entra
en un assert ni en un mensaje: lo que haya que comprobar sobre ella se
calcula antes y se afirma el booleano. Tampoco puede salir en ninguna
respuesta, en los logs ni en error_log.

El servidor se monta sin lifespan (httpx.ASGITransport, en el mismo bucle
que el PRIM contado): lo que hace el lifespan lo hacen `servidor.arrancar`
(BD y prim.startup) y logs.setup(). La conexion llega de 127.0.0.1, asi que
pasa por TRAJET_ADMIN_PEERS=auto como en Umbrel.
"""
from __future__ import annotations

import json
import logging
import sqlite3

import httpx
import pytest

pytestmark = pytest.mark.real

CLAVE_FALSA = "clave0falsa0del0panel0de0pruebas0Zz9"
SAINT_LAZARE = "STIF:StopArea:SP:71370:"
PANEL = {"X-Trajet-Panel": "1"}


def _trozos(clave: str, n: int = 8) -> set[str]:
    return {clave[i:i + n] for i in range(len(clave) - n + 1)}


async def test_clave_por_el_panel_de_principio_a_fin(servidor, clave_real, monkeypatch, caplog):
    from app import logs, prim
    from app.config import settings
    from app.main import create_app
    from app.prim import PrimError

    monkeypatch.setenv("TRAJET_ADMIN_PEERS", "auto")
    caplog.set_level(logging.DEBUG)
    c = await servidor.arrancar("")                 # sin PRIM_API_KEY en el entorno
    logs.setup()
    textos: list[str] = []

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                                 base_url="http://testserver") as api:
        async def llamar(method: str, url: str, **kw) -> httpx.Response:
            r = await api.request(method, url, **kw)
            textos.append(r.text)
            return r

        info = (await llamar("GET", "/api/admin/prim-key")).json()
        assert info["configured"] is False and info["source"] == "none"

        # 1) Una clave que PRIM no conoce: 422, el motivo por API y nada guardado.
        r = await llamar("POST", "/api/admin/prim-key", json={"key": CLAVE_FALSA}, headers=PANEL)
        assert r.status_code == 422
        res = r.json()
        resumen = [(ch["api"], ch["status"], ch["message"]) for ch in res["checks"]]
        assert res["saved"] is False and res["error"]["code"] == "prim_key_rejected", resumen
        assert [ch["api"] for ch in res["checks"]] == ["stop-monitoring", "general-message", "navitia"]
        assert all(ch["status"] == 401 and "no válida" in ch["message"] for ch in res["checks"]), resumen
        assert res["info"]["source"] == "none" and not c.has_key()

        # 2) La real: validada, cifrada con la semilla, guardada y en uso.
        r = await llamar("POST", "/api/admin/prim-key", json={"key": str(clave_real)}, headers=PANEL)
        res = r.json()
        resumen = [(ch["api"], ch["status"], ch["message"]) for ch in res["checks"]]
        assert r.status_code == 200 and res["saved"] is True, resumen
        info = res["info"]
        assert info["source"] == "panel" and info["state"] == "valid"
        assert info["encryption"] == "app_seed" and info["env_available"] is False
        ultimos = info["last4"] == clave_real[-4:]
        assert ultimos
        en_uso = c._key == clave_real
        assert en_uso
        guardada_1 = info["saved_at"]
        # El servidor arranco sin clave y ya la usa, sin reiniciar.
        data, edad = await c.stop_monitoring(SAINT_LAZARE)
        assert edad == 0.0 and "Siri" in data and c.last_error is None

        # 3) Reemplazar (la misma otra vez): se vuelve a probar y se aplica en
        # caliente (cache vacia y generacion nueva).
        gen = c._gen
        r = await llamar("POST", "/api/admin/prim-key", json={"key": str(clave_real)}, headers=PANEL)
        res = r.json()
        assert r.status_code == 200 and res["saved"] is True
        assert res["info"]["source"] == "panel" and res["info"]["saved_at"] >= guardada_1
        assert c._gen == gen + 1 and not c._cache
        en_uso = c._key == clave_real
        assert en_uso

        # 4) Recomprobar la clave en uso.
        r = await llamar("POST", "/api/admin/prim-key/check", headers=PANEL)
        res = r.json()
        resumen = [(ch["api"], ch["status"], ch["message"]) for ch in res["checks"]]
        assert r.status_code == 200 and res["saved"] is False and "error" not in res, resumen
        assert all(ch["ok"] for ch in res["checks"]) and res["info"]["state"] == "valid"

        # 5) Borrar (con la confirmacion): sin clave de entorno, a «ninguna».
        r = await llamar("DELETE", "/api/admin/prim-key", headers=PANEL)
        assert r.status_code == 400                     # sin X-Trajet-Confirm no se borra
        r = await llamar("DELETE", "/api/admin/prim-key", headers={**PANEL, "X-Trajet-Confirm": "borrar"})
        info = r.json()
        assert r.status_code == 200 and info["source"] == "none" and info["configured"] is False
        assert not c.has_key()
        with pytest.raises(PrimError) as e:              # y ya no se llama a PRIM
            await c.stop_monitoring("STIF:StopArea:SP:65063:")
        assert e.value.kind == "no_key"

        for url in ("/api/admin/overview", "/api/admin/quota", "/api/admin/errors?limit=200",
                    "/api/admin/prim-key", "/"):
            await llamar("GET", url)

    assert len(servidor.transporte.hechas) == 13, servidor.transporte.hechas
    assert servidor.transporte.hechas.count("stop-monitoring") == 5

    # La clave real no aparece en ninguna respuesta, log, error_log ni en la BD.
    con = sqlite3.connect(settings.db_path)
    try:
        textos.append("\n".join(con.iterdump()))
    finally:
        con.close()
    textos.append(caplog.text)
    textos.append(json.dumps([prim.prim_state(), prim.server_state()]))
    todo = "\n".join(textos)
    fuga = any(t in todo for t in _trozos(str(clave_real)))
    assert not fuga, "un trozo de la clave real aparece en respuestas, logs o BD"
    fuga_falsa = any(t in todo for t in _trozos(CLAVE_FALSA))
    assert not fuga_falsa
