# Imagen del servidor Trajet: la API del iPhone (/api/v1), el panel (/ y
# /api/admin) y la API de la 0.3.0 (/api) por compatibilidad.
#
# Se construye y se publica EN EL PROPIO UMBREL con scripts/publish.sh (ver el
# README). La integracion continua solo comprueba que se construye y arranca.
#
# Dos etapas. En la primera se instalan las dependencias en un venv; la final
# copia ese venv y el codigo de app/, y nada mas: ni las caches de pip, ni
# tests/, ni tools/, ni probe/ (la imagen de la 0.3.0 llevaba tools/, con
# scripts que gastan cuota de PRIM y no pintan nada en produccion).

ARG PYTHON_IMAGE=python:3.12-slim

# ---------------------------------------------------------------- construccion
FROM ${PYTHON_IMAGE} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Todas las dependencias traen rueda para x86_64 y arm64 (cryptography,
# uvloop, httptools, pydantic-core): no hace falta compilador.
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && /opt/venv/bin/pip uninstall --yes pip

# ---------------------------------------------------------------------- final
FROM ${PYTHON_IMAGE}

# TZ solo afecta a la hora de los logs: el codigo trabaja siempre con
# ZoneInfo("Europe/Paris") y con UTC para la cuota (docs/servidor.md 13).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris \
    PATH=/opt/venv/bin:$PATH \
    TRAJET_DB=/data/trajet.db

# Usuario sin privilegios con uid/gid 1000: es el dueno de ${APP_DATA_DIR} en
# Umbrel, asi que la carpeta de datos montada en /data se puede escribir sin
# tocar permisos. /data se crea aqui con ese dueno para que un volumen con
# nombre nuevo herede el dueno correcto (Docker copia el de la imagen).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 trajet \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin trajet \
 && mkdir -p /data \
 && chown 1000:1000 /data \
 && chmod 0750 /data

COPY --from=build /opt/venv /opt/venv

WORKDIR /srv
# El codigo es de root y de solo lectura para el proceso: si algo fuese mal
# dentro, no podria reescribirse a si mismo. Se compila a bytecode aqui porque
# en marcha no se escribe nada fuera de /data (PYTHONDONTWRITEBYTECODE).
COPY app/ /srv/app/
RUN python -m compileall -q /srv/app

USER 1000:1000

# Base de datos, clave PRIM cifrada (secrets/) y copias de las migraciones.
VOLUME ["/data"]
EXPOSE 8000

# /api/v1/ping y no /api/health: ping no toca la BD, ni PRIM, ni Ollama.
# /api/health llama a Ollama con 5 s de espera y un Ollama lento marcaba el
# contenedor como enfermo (docs/servidor.md 18.4). Sin curl en la imagen:
# el propio Python lo hace con urllib, y cualquier error sale con codigo 1.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ping', timeout=3)"]

# UN SOLO proceso y un solo worker: la cache de PRIM, el contador de cuota,
# el recolector de andenes y los limites del emparejamiento viven en la
# memoria del proceso. Con dos workers habria dos caches, dos recolectores y
# el doble de llamadas a PRIM. `--workers 1` explicito para que un
# WEB_CONCURRENCY en el entorno no lo cambie.
#
# --no-proxy-headers: uvicorn trae las cabeceras de proxy ENCENDIDAS por
# defecto y, si se fia de X-Forwarded-For, reescribe request.client con lo
# que diga la cabecera. auth.py decide por su cuenta de quien fiarse: mira la
# conexion real (request.client) para saber si viene del proxy de Umbrel y
# solo entonces lee X-Forwarded-For (auth.client_ip). Con --proxy-headers y
# --forwarded-allow-ips='*' el panel veria la IP del navegador en vez de la del
# proxy (403 legitimo) y cualquier contenedor de la red compartida entraria al
# panel mandando «X-Forwarded-For: 127.0.0.1». tests/test_empaquetado.py lo
# demuestra.
#
# El log de acceso se queda: logs.py ya le quita las query strings.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
