"""Clave PRIM guardada desde el panel: cifrada en reposo y con prioridad
sobre la del entorno (R83 en parte, R84)."""
from __future__ import annotations

import base64
import json
import logging
import os
import stat

import pytest
import yaml
from jsonschema import Draft202012Validator

from app.keystore import KeyStore

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contract")

CLAVE = "clave0de0prueba0para0el0panel01"
OTRA = "otra0clave0de0prueba0distinta02"
ENTORNO = "clave0del0entorno0de0pruebas003"
POSIX = os.name == "posix"


def _info_validator() -> Draft202012Validator:
    with open(os.path.join(DOCS, "openapi.yaml"), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    return Draft202012Validator({"$ref": "#/components/schemas/PrimKeyInfo",
                                 "components": spec["components"]})


def _fichero(tmp_path) -> bytes:
    return (tmp_path / "secrets" / "prim-key.json").read_bytes()


def _formas(clave: str) -> list[bytes]:
    """La clave y las codificaciones con las que podria colarse."""
    raw = clave.encode()
    return [raw, base64.b64encode(raw), base64.b64encode(raw).rstrip(b"="),
            base64.urlsafe_b64encode(raw), raw.hex().encode(), raw.hex().upper().encode()]


def test_keystore_prioridad_y_cifrado(tmp_path):
    """R84: el fichero no lleva la clave (ni en claro ni en base64) y otra
    instancia (un reinicio) la recupera con la misma semilla."""
    ks = KeyStore(str(tmp_path), "semilla-del-umbrel", ENTORNO)
    assert ks.current() == (ENTORNO, "env")
    ks.save(CLAVE, "valid", "las tres APIs responden")
    assert ks.current() == (CLAVE, "panel")

    raw = _fichero(tmp_path)
    for forma in _formas(CLAVE):
        assert forma not in raw
    doc = json.loads(raw)
    assert doc["kdf"] == "HKDF-SHA256" and doc["alg"] == "AES-256-GCM"
    assert doc["enc"] == "app_seed" and doc["last4"] == CLAVE[-4:]
    assert len(base64.b64decode(doc["salt"])) == 16
    assert len(base64.b64decode(doc["nonce"])) == 12
    assert not [p for p in os.listdir(tmp_path / "secrets") if ".tmp" in p]

    otra = KeyStore(str(tmp_path), "semilla-del-umbrel", ENTORNO)
    assert otra.current() == (CLAVE, "panel")
    info = otra.info()
    _info_validator().validate(info)
    assert info == {**info, "configured": True, "source": "panel", "last4": CLAVE[-4:],
                    "state": "valid", "env_available": True, "encryption": "app_seed"}
    assert CLAVE not in json.dumps(info)


def test_mismo_texto_cifrado_distinto_cada_vez(tmp_path):
    """Salt y nonce nuevos en cada guardado: dos guardados de la misma clave
    no dejan el mismo fichero."""
    ks = KeyStore(str(tmp_path), "semilla", "")
    ks.save(CLAVE)
    uno = json.loads(_fichero(tmp_path))
    ks.save(CLAVE)
    dos = json.loads(_fichero(tmp_path))
    assert uno["ct"] != dos["ct"] and uno["salt"] != dos["salt"]
    assert uno["nonce"] != dos["nonce"]


@pytest.mark.skipif(not POSIX, reason="permisos POSIX (en Windows no hay 0600)")
def test_permisos_0700_y_0600(tmp_path):
    ks = KeyStore(str(tmp_path), "", "")
    ks.save(CLAVE)
    carpeta = tmp_path / "secrets"
    assert stat.S_IMODE(os.stat(carpeta).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(carpeta / "prim-key.json").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(carpeta / "master.key").st_mode) == 0o600


def test_reemplazo(tmp_path):
    ks = KeyStore(str(tmp_path), "semilla", ENTORNO)
    ks.save(CLAVE, "valid")
    antes = ks.info()
    ks.save(OTRA, "unknown")
    assert ks.current() == (OTRA, "panel")
    assert ks.info()["last4"] == OTRA[-4:] and ks.info()["state"] == "unknown"
    assert ks.info()["saved_at"] >= antes["saved_at"]
    raw = _fichero(tmp_path)
    for forma in _formas(OTRA) + _formas(CLAVE):
        assert forma not in raw
    assert KeyStore(str(tmp_path), "semilla", ENTORNO).current() == (OTRA, "panel")


def test_borrado_vuelve_al_entorno_o_a_nada(tmp_path):
    ks = KeyStore(str(tmp_path), "semilla", ENTORNO)
    ks.save(CLAVE)
    ks.delete()
    assert not (tmp_path / "secrets" / "prim-key.json").exists()
    assert ks.current() == (ENTORNO, "env")
    assert ks.info()["source"] == "env" and ks.info()["encryption"] == "none"
    assert KeyStore(str(tmp_path), "semilla", ENTORNO).current() == (ENTORNO, "env")

    sin_entorno = KeyStore(str(tmp_path), "semilla", "")
    assert sin_entorno.current() == ("", "none")
    info = sin_entorno.info()
    _info_validator().validate(info)
    assert info["configured"] is False and info["state"] == "missing"
    assert info["last4"] is None
    sin_entorno.delete()                        # borrar sin nada no falla


def test_semilla_cambiada_se_avisa_y_se_usa_el_entorno(tmp_path, caplog):
    KeyStore(str(tmp_path), "semilla-vieja", ENTORNO).save(CLAVE, "valid")
    caplog.set_level(logging.DEBUG)
    ks = KeyStore(str(tmp_path), "semilla-nueva", ENTORNO)
    assert ks.current() == (ENTORNO, "env")
    info = ks.info()
    assert info["source"] == "env" and "descifrar" in info["state_detail"]
    assert any("no se puede descifrar" in r.getMessage() for r in caplog.records)
    assert CLAVE not in caplog.text and ENTORNO not in caplog.text
    # El fichero no se borra solo: si vuelve la semilla buena, vuelve la clave.
    assert KeyStore(str(tmp_path), "semilla-vieja", ENTORNO).current() == (CLAVE, "panel")
    # Sin entorno: no hay clave, pero no se tumba nada.
    assert KeyStore(str(tmp_path), "semilla-nueva", "").current() == ("", "none")
    # Guardarla de nuevo arregla el aviso.
    ks.save(OTRA)
    assert ks.current() == (OTRA, "panel") and "descifrar" not in ks.info()["state_detail"]


def test_fichero_roto_no_tumba_nada(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "prim-key.json").write_text("{esto no es json", encoding="utf-8")
    ks = KeyStore(str(tmp_path), "semilla", ENTORNO)
    assert ks.current() == (ENTORNO, "env")
    assert "descifrar" in ks.info()["state_detail"]


def test_sin_semilla_clave_maestra_local(tmp_path):
    ks = KeyStore(str(tmp_path), "", "")
    ks.save(CLAVE)
    master = tmp_path / "secrets" / "master.key"
    assert master.exists() and len(master.read_bytes()) == 32
    assert ks.info()["encryption"] == "local_master_key"
    assert json.loads(_fichero(tmp_path))["enc"] == "local_master_key"
    assert CLAVE.encode() not in _fichero(tmp_path)
    assert KeyStore(str(tmp_path), "", "").current() == (CLAVE, "panel")
    # Si luego aparece APP_SEED, la guardada se sigue leyendo con la maestra
    # y la siguiente se guarda ya con la semilla.
    con_semilla = KeyStore(str(tmp_path), "semilla", "")
    assert con_semilla.current() == (CLAVE, "panel")
    con_semilla.save(OTRA)
    assert con_semilla.info()["encryption"] == "app_seed"
    # Sin la maestra, una clave guardada con ella ya no se puede leer.
    KeyStore(str(tmp_path), "", "").save(CLAVE)
    master.unlink()
    assert KeyStore(str(tmp_path), "", ENTORNO).current() == (ENTORNO, "env")


def test_estado_de_la_clave(tmp_path):
    ks = KeyStore(str(tmp_path), "semilla", ENTORNO)
    assert ks.info()["state"] == "unknown" and ks.info()["checked_at"] is None
    ks.set_state("invalid", "PRIM respondió 401")
    assert ks.state()[:2] == ("invalid", "PRIM respondió 401")
    ks.save(CLAVE, "valid")
    ks.set_state("forbidden", "sin permiso para: navitia")
    # El estado de la del panel se guarda en su fichero (sobrevive al reinicio).
    otra = KeyStore(str(tmp_path), "semilla", ENTORNO)
    assert otra.info()["state"] == "forbidden"
    assert otra.info()["state_detail"] == "sin permiso para: navitia"
    assert otra.info()["checked_at"]
    with pytest.raises(ValueError):
        ks.set_state("rara")
    with pytest.raises(ValueError):
        ks.save("   ")


def test_get_store_sigue_a_la_configuracion(env, monkeypatch):
    from app import keystore
    from app.config import settings
    a = keystore.get_store()
    assert keystore.get_store() is a
    assert a.current()[1] == "env"
    monkeypatch.setenv("PRIM_API_KEY", "")
    settings.reload()
    b = keystore.get_store()
    assert b is not a and b.current() == ("", "none")
    keystore.reset_store()
    assert keystore.get_store() is not b
