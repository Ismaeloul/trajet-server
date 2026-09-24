"""Empaquetado: Dockerfile, .dockerignore, docker-compose.yml, scripts y README.

No son pruebas de estilo: cada una guarda una decision de seguridad o de
despliegue que un cambio despistado romperia sin que fallase ningun otro test.

  - La imagen arranca uvicorn SIN cabeceras de proxy y con un solo worker.
    auth.py decide por su cuenta de quien fiarse (auth.client_ip); el test
    del middleware de uvicorn demuestra que activarlas abriria el panel a
    cualquier contenedor de la red de Umbrel y dejaria fuera al dueno.
  - Usuario 1000 (el dueno de ${APP_DATA_DIR} en Umbrel), datos en /data y
    nada de tests/, tools/ ni probe/ en la imagen.
  - Ni secretos ni datos en el contexto de `docker build`.
  - El compose de desarrollo solo escucha en 127.0.0.1.
  - scripts/publish.sh: sintaxis, finales de linea LF (corre en el Umbrel) y
    errores claros antes de tocar Docker.
  - El README documenta todas las variables de entorno que lee el servidor.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest
import yaml
from _seg_contrato import PeerApp
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import auth
from app.api import errors
from app.config import VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _leer(*partes: str) -> str:
    with open(os.path.join(ROOT, *partes), encoding="utf-8") as f:
        return f.read()


def _instrucciones_finales() -> list[str]:
    """Instrucciones de la ULTIMA etapa del Dockerfile, con las lineas
    partidas con `\\` ya unidas y sin comentarios."""
    logicas, actual = [], ""
    for linea in _leer("Dockerfile").splitlines():
        s = linea.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            actual += s[:-1] + " "
            continue
        logicas.append(actual + s)
        actual = ""
    ultima = max(i for i, l in enumerate(logicas) if l.upper().startswith("FROM "))
    return logicas[ultima:]


def _instruccion(nombre: str) -> list[str]:
    return [l for l in _instrucciones_finales() if l.split(None, 1)[0].upper() == nombre]


# ---------------- Dockerfile ----------------

def test_dockerfile_uvicorn_sin_cabeceras_de_proxy_y_un_worker():
    (cmd,) = _instruccion("CMD")
    args = json.loads(cmd.split(None, 1)[1])
    assert args[:2] == ["uvicorn", "app.main:app"]
    # Dentro del contenedor: lo que se publica fuera lo decide el compose.
    assert args[args.index("--host") + 1] == "0.0.0.0"  # noqa: S104
    assert args[args.index("--port") + 1] == "8000"
    # Un solo proceso: cache, cuota y recolector viven en memoria. Explicito
    # para que WEB_CONCURRENCY no lo cambie.
    assert args[args.index("--workers") + 1] == "1"
    # uvicorn trae las cabeceras de proxy encendidas por defecto: se apagan.
    assert "--no-proxy-headers" in args
    assert "--proxy-headers" not in args
    assert not any(a.startswith("--forwarded-allow-ips") for a in args)
    # El log de acceso se queda (logs.py le quita las query strings).
    assert "--no-access-log" not in args


def test_dockerfile_usuario_1000_datos_en_volumen_y_solo_app():
    assert _instruccion("USER") == ["USER 1000:1000"]
    assert _instruccion("VOLUME") == ['VOLUME ["/data"]']
    assert _instruccion("EXPOSE") == ["EXPOSE 8000"]
    env = " ".join(_instruccion("ENV"))
    for par in ("TRAJET_DB=/data/trajet.db", "PYTHONDONTWRITEBYTECODE=1",
                "PYTHONUNBUFFERED=1", "TZ=Europe/Paris"):
        assert par in env
    # /data con el dueno correcto para que un volumen nuevo lo herede.
    assert any("chown 1000:1000 /data" in l for l in _instruccion("RUN"))
    # A la imagen final solo llegan el venv y app/.
    copias = [l.split()[1:] for l in _instruccion("COPY")]
    assert copias == [["--from=build", "/opt/venv", "/opt/venv"], ["app/", "/srv/app/"]]
    todo = _leer("Dockerfile")
    for fuera in ("COPY tests", "COPY tools", "COPY probe", "COPY . "):
        assert fuera not in todo


def test_healthcheck_sin_curl_contra_ping():
    (hc,) = _instruccion("HEALTHCHECK")
    args = json.loads(hc[hc.index("CMD") + 3:].strip())
    assert args[:2] == ["python", "-c"]
    codigo = args[2]
    compile(codigo, "<healthcheck>", "exec")
    # ping: no toca la BD, ni PRIM, ni Ollama (y nunca pide token).
    assert "http://127.0.0.1:8000/api/v1/ping" in codigo
    assert "timeout=" in codigo
    assert "curl" not in hc
    assert "--timeout=5s" in hc


def test_dockerignore_deja_fuera_secretos_datos_y_pruebas():
    lineas = {l.strip() for l in _leer(".dockerignore").splitlines()
              if l.strip() and not l.strip().startswith("#")}
    for patron in (".env*", "data", ".local", "*.db", "**/*.db", ".venv", "tests", "tools",
                   "probe", ".git", "__pycache__", "**/__pycache__"):
        assert patron in lineas, patron
    # Lo que la imagen SI necesita no puede quedar fuera.
    assert not lineas & {"app", "app/", "requirements.txt", "*.txt", "*.py", "**/*.py"}


# ---------------- por que no --proxy-headers ----------------

@pytest.fixture
def umbrel_route(tmp_path, monkeypatch):
    """/proc/net/route con la puerta de enlace de Umbrel (10.21.0.1)."""
    f = tmp_path / "route"
    f.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0100150A\t0003\t0\t0\t0\t00000000\t0\t0\t0\n", encoding="ascii")
    monkeypatch.setattr(auth, "PROC_ROUTE", str(f))
    monkeypatch.delenv("APP_PROXY_HOSTNAME", raising=False)
    monkeypatch.setattr(auth.settings, "admin_peers", "auto")
    auth.reset_state()
    return f


def test_cabeceras_de_proxy_de_uvicorn_abririan_el_panel(env, umbrel_route):
    app = FastAPI()
    errors.install(app)

    @app.get("/api/admin/prueba", dependencies=[Depends(auth.require_panel)])
    async def leer():
        return {"ok": True}

    # Como arranca la imagen (--no-proxy-headers): manda la conexion real.
    como_la_imagen = TestClient(PeerApp(app))
    # Como seria con --proxy-headers --forwarded-allow-ips='*': uvicorn
    # reescribe la conexion con lo que diga X-Forwarded-For.
    con_cabeceras = TestClient(PeerApp(ProxyHeadersMiddleware(app, trusted_hosts="*")))

    # Otra app de la red compartida de Umbrel que se inventa la cabecera.
    intruso = {"x-test-peer": "10.21.0.7", "X-Forwarded-For": "127.0.0.1"}
    # El proxy de Umbrel, que pone la IP del navegador del dueno.
    dueno = {"x-test-peer": "10.21.0.1", "X-Forwarded-For": "192.168.1.40"}

    assert como_la_imagen.get("/api/admin/prueba", headers=intruso).status_code == 403
    assert como_la_imagen.get("/api/admin/prueba", headers=dueno).status_code == 200

    assert con_cabeceras.get("/api/admin/prueba", headers=intruso).status_code == 200
    assert con_cabeceras.get("/api/admin/prueba", headers=dueno).status_code == 403


# ---------------- docker-compose.yml (desarrollo) ----------------

def test_compose_de_desarrollo_como_el_umbrel_y_solo_en_localhost():
    compose = yaml.safe_load(_leer("docker-compose.yml"))
    (svc,) = compose["services"].values()
    assert svc["build"] == "."
    assert svc["ports"] and all(str(p).startswith("127.0.0.1:") for p in svc["ports"])
    assert "127.0.0.1:7796:8000" in svc["ports"]
    assert svc["user"] == "1000:1000"
    assert svc["init"] is True
    assert svc["mem_limit"] == "384m"
    assert svc["pids_limit"] == 128
    assert "./data:/data" in svc["volumes"]
    assert svc["env_file"] == [{"path": ".env", "required": False}]
    envs = svc["environment"]
    assert envs["TRAJET_ADMIN_PEERS"] == "auto"
    assert envs["TRAJET_DB"] == "/data/trajet.db"
    assert envs["TRAJET_SECRET_SEED"] == "${TRAJET_SECRET_SEED:-}"
    # La clave nunca en el compose: va en el panel (o en .env).
    assert "PRIM_API_KEY" not in envs


# ---------------- scripts ----------------

def _bash() -> str:
    exe = shutil.which("bash")
    if not exe:
        pytest.skip("no hay bash en este equipo")
    try:
        r = subprocess.run([exe, "-c", "echo ok"],  # noqa: S603 (bash del sistema, orden fija)
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("bash no arranca")
    if r.returncode != 0 or r.stdout.strip() != "ok":
        pytest.skip("bash no funciona (¿WSL sin distribucion?)")
    return exe


def _scripts() -> list[str]:
    carpeta = os.path.join(ROOT, "scripts")
    return sorted(f"scripts/{n}" for n in os.listdir(carpeta) if n.endswith(".sh"))


def test_scripts_con_finales_lf():
    # Corren en Linux (el Umbrel, el runner de GitHub): un CRLF rompe
    # `set -euo pipefail` con un error que no dice nada.
    for s in _scripts():
        with open(os.path.join(ROOT, s), "rb") as f:
            datos = f.read()
        assert b"\r\n" not in datos, s
        assert datos.startswith(b"#!/usr/bin/env bash\n"), s


def test_scripts_sintaxis_bash():
    bash = _bash()
    for s in _scripts():
        r = subprocess.run([bash, "-n", s], cwd=ROOT,  # noqa: S603 (scripts del repo)
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{s}: {r.stderr}"


def _publish(bash: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([bash, "scripts/publish.sh", *args], cwd=ROOT,  # noqa: S603
                          capture_output=True, text=True, timeout=60)


def test_publish_ayuda_y_errores_claros_antes_de_tocar_docker():
    bash = _bash()
    r = _publish(bash, "--help")
    assert r.returncode == 0, r.stderr
    assert "Uso" in r.stdout and "localhost:5000" in r.stdout

    r = _publish(bash, "abc")
    assert r.returncode != 0
    assert "version no valida" in r.stderr

    # Una version distinta de la del codigo: el servidor anunciaria otra.
    r = _publish(bash, f"{VERSION}-otra")
    assert r.returncode != 0
    assert f"app/config.py dice {VERSION}" in r.stderr

    r = _publish(bash, "--raro")
    assert r.returncode != 0
    assert "opcion desconocida" in r.stderr

    r = _publish(bash, "1.0.0", "2.0.0")
    assert r.returncode != 0
    assert "sobra un argumento" in r.stderr


# ---------------- documentacion ----------------

def _variables_del_servidor() -> set[str]:
    nombres = set(re.findall(r'_(?:env|flag|int)\("([A-Z0-9_]+)"', _leer("app", "config.py")))
    for mod in ("auth.py", "keystore.py", "logs.py", "prim.py", "mapdata.py", "main.py"):
        ruta = os.path.join(ROOT, "app", mod)
        if os.path.exists(ruta):
            nombres |= set(re.findall(r'os\.environ(?:\.get)?\(\s*"([A-Z0-9_]+)"', _leer("app", mod)))
            nombres |= set(re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"', _leer("app", mod)))
    return nombres


def test_readme_documenta_todas_las_variables_de_entorno():
    nombres = _variables_del_servidor()
    assert {"PRIM_API_KEY", "TRAJET_DB", "TRAJET_SECRET_SEED", "TRAJET_ADMIN_PEERS"} <= nombres
    readme = _leer("README.md")
    faltan = sorted(n for n in nombres if f"`{n}`" not in readme)
    assert not faltan, f"variables sin documentar en README.md: {faltan}"


def test_env_example_sin_clave_ni_direcciones_reales():
    texto = _leer(".env.example")
    valores = dict(l.split("=", 1) for l in texto.splitlines()
                   if l.strip() and not l.lstrip().startswith("#") and "=" in l)
    assert valores.get("PRIM_API_KEY", "") == ""
    # Las direcciones del QR se ponen en el panel: aqui no va ninguna IP de
    # verdad (ni privada ni de Tailscale).
    privadas = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01])|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7]))"
                          r"\.\d{1,3}\.\d{1,3}\b")
    assert not privadas.search(texto)
