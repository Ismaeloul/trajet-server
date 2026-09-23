"""Ayudas de los tests de seguridad y v1: el contrato y un cliente que lo vigila.

`ContractClient` envuelve el TestClient: cada respuesta de /api/v1 se valida
contra docs/openapi.yaml (copia en tests/contract/openapi.yaml) para la
operacion y el codigo de estado que haya salido. Un estado que el contrato no
declara para esa operacion tambien es un fallo. Asi «cada respuesta de los
tests valida con el contrato» no depende de acordarse de llamar a nada.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from functools import lru_cache
from urllib.parse import urlsplit

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACT = os.path.join(HERE, "contract", "openapi.yaml")
METHODS = ("get", "post", "put", "patch", "delete")


@lru_cache(maxsize=1)
def spec() -> dict:
    with open(CONTRACT, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _format_checker() -> FormatChecker:
    """date-time y uri de verdad (jsonschema no los mira sin paquetes extra)."""
    fc = FormatChecker()

    @fc.checks("date-time", raises=ValueError)
    def _dt(value) -> bool:
        if not isinstance(value, str):
            return True
        if "T" not in value:
            raise ValueError("sin T")
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("sin zona horaria")
        return True

    @fc.checks("uri", raises=ValueError)
    def _uri(value) -> bool:
        if not isinstance(value, str):
            return True
        p = urlsplit(value)
        if not p.scheme or not (p.netloc or p.path):
            raise ValueError("no es una URI")
        return True

    return fc


_FC = _format_checker()


def validator(schema: dict) -> Draft202012Validator:
    """Valida `schema` resolviendo los $ref de components del contrato."""
    root = dict(schema)
    root["components"] = spec()["components"]
    return Draft202012Validator(root, format_checker=_FC)


def schema_validator(name: str) -> Draft202012Validator:
    return validator({"$ref": f"#/components/schemas/{name}"})


def v1_operations() -> list[tuple[str, str, dict]]:
    """(path, metodo, operacion) de todo /api/v1 del contrato."""
    out = []
    for path, item in spec()["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method in METHODS:
            if method in item:
                op = dict(item[method])
                op["_path_params"] = item.get("parameters", [])
                out.append((path, method, op))
    return out


def _template_regex(path: str) -> re.Pattern:
    return re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path)) + "$")


def find_operation(method: str, path: str) -> tuple[str, dict] | None:
    """La operacion del contrato para una peticion concreta (las rutas
    literales ganan a las de plantilla: /routes/from-plan antes que
    /routes/{route_id})."""
    method = method.lower()
    matches = []
    for tpl, m, op in v1_operations():
        if m == method and _template_regex(tpl).match(path):
            matches.append((tpl.count("{"), tpl, op))
    if not matches:
        return None
    matches.sort(key=lambda x: x[0])
    return matches[0][1], matches[0][2]


def _resolve_response(resp: dict) -> dict:
    ref = resp.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        return spec()["components"]["responses"][name]
    return resp


def check_response(method: str, url: str, response) -> None:
    """Falla si la respuesta no es la que el contrato declara."""
    path = urlsplit(url).path if "://" in url else url.split("?", 1)[0]
    found = find_operation(method, path)
    assert found is not None, f"{method.upper()} {path} no esta en el contrato"
    tpl, op = found
    status = str(response.status_code)
    responses = op.get("responses", {})
    assert status in responses, (
        f"{method.upper()} {tpl}: el contrato no declara el estado {status} "
        f"(declara {sorted(responses)}); cuerpo: {response.text[:300]}")
    decl = _resolve_response(responses[status])
    content = decl.get("content")
    if not content:
        assert response.content == b"", f"{status} sin cuerpo en el contrato"
        return
    assert "application/json" in content, f"{tpl} {status}: el contrato no es JSON"
    assert response.headers.get("content-type", "").startswith("application/json"), \
        f"{tpl} {status}: content-type {response.headers.get('content-type')}"
    errors = sorted(validator(content["application/json"]["schema"]).iter_errors(response.json()),
                    key=lambda e: list(e.absolute_path))
    assert not errors, (f"{method.upper()} {tpl} {status} no cumple el contrato:\n"
                        + "\n".join(f"  {list(e.absolute_path)}: {e.message[:300]}"
                                    for e in errors[:10]))
    for name in (decl.get("headers") or {}):
        if name.lower() == "etag" and status != "200":
            continue
        assert name.lower() in {h.lower() for h in response.headers}, \
            f"{tpl} {status}: falta la cabecera {name}"


class ContractClient:
    """TestClient que comprueba el contrato en cada respuesta de /api/v1.

    Apunta tambien que operaciones se han ejercitado (`seen`)."""

    def __init__(self, client, token: str | None = None):
        self.client = client
        self.token = token
        self.seen: set[tuple[str, str, str]] = set()

    def with_token(self, token: str | None) -> "ContractClient":
        other = ContractClient(self.client, token)
        other.seen = self.seen
        return other

    def request(self, method: str, url: str, **kw):
        headers = dict(kw.pop("headers", None) or {})
        if self.token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {self.token}"
        r = self.client.request(method.upper(), url, headers=headers, **kw)
        path = url.split("?", 1)[0]
        if path.startswith("/api/v1/"):
            check_response(method, path, r)
            found = find_operation(method, path)
            if found:
                self.seen.add((method.lower(), found[0], str(r.status_code)))
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


# ---------------- estado y red simulada ----------------

@pytest.fixture(autouse=True)
def auth_limpio():
    """Limites de emparejamiento y pares del panel vacios en cada test.

    Es autouse en los ficheros que lo importan (`from _seg_contrato import
    auth_limpio`); conftest.py no lo hace todavia."""
    from app import auth
    auth.reset_state()
    yield
    auth.reset_state()


class PeerApp:
    """Envuelve la app para que la conexion venga de la IP que diga la
    cabecera `x-test-peer` (el TestClient siempre dice «testclient»).

    Solo para tests: simula el proxy de Umbrel, otra app de la red Docker o
    el iPhone por la LAN."""

    def __init__(self, app, default: str = "testclient"):
        self.app = app
        self.default = default

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            peer = self.default
            for k, v in scope.get("headers", []):
                if k == b"x-test-peer":
                    peer = v.decode()
            scope = dict(scope, client=(peer, 50000))
        await self.app(scope, receive, send)


# ---------------- emparejar en los tests ----------------

def pair(client, name: str = "iPhone de pruebas", ip_headers: dict | None = None) -> dict:
    """Genera un codigo como lo haria el panel y lo canjea por /api/v1/pair.

    Devuelve el PairResult. No usa /api/admin (es de otro modulo): llama a
    auth.new_pairing directamente."""
    from app import auth
    session = auth.new_pairing()
    r = client.post("/api/v1/pair", json={"code": session["code"], "device_name": name,
                                          "device_model": "iPhone17,1", "app_version": "2.0"},
                    headers=ip_headers or {})
    assert r.status_code == 200, r.text
    return r.json()


def sample_path(tpl: str) -> str:
    """Una URL concreta para una plantilla del contrato."""
    return (tpl.replace("{route_id}", "1")
               .replace("{stop_id}", "stop_area:IDFM:71370"))


def sample_query(op: dict) -> dict:
    """Parametros de query obligatorios con valores validos."""
    values = {"q": "saint lazare", "from": "stop_area:IDFM:71370",
              "to": "stop_area:IDFM:65063", "line_id": "line:IDFM:C01739"}
    out = {}
    for p in op.get("parameters", []):
        if p.get("in") == "query" and p.get("required"):
            out[p["name"]] = values[p["name"]]
    return out
