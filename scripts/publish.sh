#!/usr/bin/env bash
# Construye la imagen del servidor EN EL PROPIO UMBREL y la publica en el
# registro local del NAS. Nada sale de la maquina.
#
# Uso, por SSH en el Umbrel, desde la carpeta de trajet-server:
#
#   bash scripts/publish.sh                 # la version de app/config.py (VERSION)
#   bash scripts/publish.sh 0.4.0           # una version concreta
#   bash scripts/publish.sh 0.4.0 --latest  # y ademas la etiqueta :latest
#
# Si tu usuario no esta en el grupo docker (en el Umbrel, `umbrel` no lo
# esta), lanzalo con sudo: `sudo bash scripts/publish.sh 0.4.0`.
#
# Deja la imagen en localhost:5000/ismaeloul-trajet/trajet:<version>, que es la
# que pide el docker-compose.yml de la app en la tienda. Despues: actualizar
# (o instalar) Trajet desde la tienda de Umbrel. Pasos completos en el README.
#
# Opciones:
#   --latest   etiqueta tambien :latest (la tienda NO la usa: pide la version).
#   --force    publica aunque la version no sea la de app/config.py.
#   -h/--help  esta ayuda.
#
# Variables (para probar fuera del Umbrel):
#   REGISTRY            registro donde publicar (localhost:5000)
#   REGISTRY_CONTAINER  nombre del contenedor del registro si hay que crearlo
#                       (trajet-registry)
#
# POR QUE HACE FALTA UN REGISTRO LOCAL
# Umbrel no arranca la app con `docker compose up` a secas: antes hace un
# `docker pull` de cada `image:` del compose, una por una, y si alguna falla
# aborta la instalacion. Ese pull IGNORA `pull_policy: never`, asi que no basta
# con tener la imagen construida: tiene que existir un registro del que
# descargarla. El registro va atado a 127.0.0.1, o sea que no se expone a la
# red, y Docker acepta localhost como registro inseguro sin configurar nada.
# Es el mismo registro que usan ipa-station y ace-player-neo: si ya hay uno
# escuchando en el puerto, se usa ese.
set -euo pipefail

REGISTRY="${REGISTRY:-localhost:5000}"
REGISTRY_CONTAINER="${REGISTRY_CONTAINER:-trajet-registry}"
REPO_NAME="ismaeloul-trajet/trajet"
IMAGE="${REGISTRY}/${REPO_NAME}"

cd "$(dirname "$0")/.."

uso () {
  # La cabecera de este fichero, sin las almohadillas, hasta `set -euo`.
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

falla () {
  echo >&2
  echo "ERROR: $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Argumentos
# ---------------------------------------------------------------------------
VERSION=""
LATEST=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    -h|--help) uso; exit 0 ;;
    --latest)  LATEST=1 ;;
    --force)   FORCE=1 ;;
    -*)        falla "opcion desconocida: $arg (mira: bash scripts/publish.sh --help)" ;;
    *)
      [ -z "$VERSION" ] || falla "sobra un argumento: $arg (solo va una version)"
      VERSION="$arg"
      ;;
  esac
done

[ -f Dockerfile ] && [ -f app/config.py ] && [ -f requirements.txt ] \
  || falla "no encuentro Dockerfile, app/config.py y requirements.txt: lanzalo desde el repo trajet-server"

# La version que anuncia el servidor (/api/v1/ping, el panel) sale de aqui.
CODE_VERSION=$(sed -n 's/^VERSION *= *"\([^"]*\)".*/\1/p' app/config.py | head -1)
[ -n "$CODE_VERSION" ] || falla "no encuentro VERSION = \"x.y.z\" en app/config.py"
VERSION="${VERSION:-$CODE_VERSION}"

# Una etiqueta de Docker admite mas cosas, pero la tienda y el README hablan de
# versiones x.y.z (con sufijo opcional, p. ej. 0.4.0-rc1).
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] \
  || falla "version no valida: '$VERSION' (tiene que ser x.y.z, p. ej. 0.4.0)"

if [ "$VERSION" != "$CODE_VERSION" ] && [ "$FORCE" != 1 ]; then
  falla "pides la $VERSION pero app/config.py dice $CODE_VERSION: el servidor anunciaria
       la $CODE_VERSION en el panel y en el iPhone. Cambia VERSION en app/config.py o,
       si sabes lo que haces, repite con --force."
fi

# ---------------------------------------------------------------------------
# Requisitos
# ---------------------------------------------------------------------------
echo "==> Comprobando requisitos"
command -v docker >/dev/null || falla "falta docker"
command -v curl >/dev/null || falla "falta curl (hace falta para hablar con el registro)"
if ! docker info >/dev/null 2>&1; then
  if [ "$(id -u)" != 0 ]; then
    falla "el demonio de docker no responde o tu usuario no puede usarlo.
       Prueba con sudo: sudo bash scripts/publish.sh $*"
  fi
  falla "el demonio de docker no responde"
fi

REG_HOST="${REGISTRY%:*}"
REG_PORT="${REGISTRY##*:}"
case "$REG_HOST" in
  localhost|127.0.0.1) ;;
  *) falla "REGISTRY tiene que ser local (localhost:<puerto>): es lo que acepta Docker sin TLS y lo que pide la tienda" ;;
esac
[[ "$REG_PORT" =~ ^[0-9]+$ ]] || falla "REGISTRY sin puerto: $REGISTRY (p. ej. localhost:5000)"
REG_URL="http://127.0.0.1:${REG_PORT}"

AVAIL_GB=$(df -BG --output=avail . 2>/dev/null | tail -1 | tr -dc '0-9' || true)
if [ -n "$AVAIL_GB" ] && [ "$AVAIL_GB" -lt 2 ]; then
  echo "AVISO: quedan ${AVAIL_GB}G libres en este disco. La imagen ocupa unos 300 MB y la"
  echo "       construccion necesita algo mas mientras tanto."
fi

# git como root en una carpeta de otro usuario se niega («dubious
# ownership»); safe.directory solo para esta orden.
REVISION=$(git -c safe.directory="$PWD" rev-parse --short HEAD 2>/dev/null || echo "sin-git")
if [ "$REVISION" != "sin-git" ] \
   && [ -n "$(git -c safe.directory="$PWD" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  echo "AVISO: hay cambios sin commitear; la imagen llevara el codigo tal como esta en disco."
  REVISION="${REVISION}-modificado"
fi
echo "    version ${VERSION} · codigo ${REVISION} · registro ${REGISTRY}"

# ---------------------------------------------------------------------------
# Registro local
# ---------------------------------------------------------------------------
registro_vivo () {
  curl -fsS -m 3 "${REG_URL}/v2/" >/dev/null 2>&1
}

ensure_registry () {
  if registro_vivo; then
    echo "==> Registro local ya en marcha en ${REGISTRY}"
    return
  fi
  # Un registro parado en ESTE puerto (el de ipa-station, el de
  # ace-player-neo o el nuestro): se arranca ese antes que crear otro, que se
  # quedaria sin sus imagenes y chocaria con el puerto al volver el primero.
  # Los que publican otro puerto no se tocan.
  local parados puertos
  parados="$(docker ps -aq --filter "ancestor=registry:2" --filter "status=exited" || true)"
  parados="${parados} $(docker ps -aq --filter "name=^${REGISTRY_CONTAINER}$" --filter "status=exited" || true)"
  for id in $parados; do
    puertos=$(docker inspect -f '{{range $p, $b := .HostConfig.PortBindings}}{{range $b}} {{.HostPort}} {{end}}{{end}}' "$id" 2>/dev/null || true)
    case "$puertos" in *" ${REG_PORT} "*) ;; *) continue ;; esac
    echo "==> Arrancando el registro parado $(docker inspect -f '{{.Name}}' "$id" | tr -d /)"
    docker start "$id" >/dev/null || true
    for _ in $(seq 1 10); do registro_vivo && return; sleep 1; done
  done
  if [ -n "$(docker ps -aq --filter "name=^${REGISTRY_CONTAINER}$")" ]; then
    falla "el contenedor ${REGISTRY_CONTAINER} existe pero el registro no responde en ${REGISTRY}.
       Mira: docker logs ${REGISTRY_CONTAINER}"
  fi
  echo "==> Creando el registro local en ${REGISTRY} (contenedor ${REGISTRY_CONTAINER})"
  docker run -d \
    --name "${REGISTRY_CONTAINER}" \
    --restart unless-stopped \
    -p "127.0.0.1:${REG_PORT}:5000" \
    -v "${REGISTRY_CONTAINER}-data:/var/lib/registry" \
    registry:2 >/dev/null
  for _ in $(seq 1 15); do registro_vivo && return; sleep 1; done
  falla "el registro local no responde en ${REGISTRY}"
}

ya_publicada () {
  # Un 404 aqui es la respuesta normal (aun no esta): nada por pantalla.
  curl -fs -m 5 \
    -H "Accept: application/vnd.oci.image.index.v1+json" \
    -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
    -H "Accept: application/vnd.oci.image.manifest.v1+json" \
    -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
    "${REG_URL}/v2/${REPO_NAME}/manifests/$1" >/dev/null 2>&1
}

ensure_registry

if ya_publicada "$VERSION"; then
  echo "AVISO: la ${VERSION} ya estaba publicada: se sustituye. Si esa version ya esta"
  echo "       instalada, Umbrel no la vuelve a bajar hasta reinstalar la app; lo normal"
  echo "       es subir la version (app/config.py y la tienda)."
fi

# ---------------------------------------------------------------------------
# Construir
# ---------------------------------------------------------------------------
TAGS=(--tag "${IMAGE}:${VERSION}")
[ "$LATEST" = 1 ] && TAGS+=(--tag "${IMAGE}:latest")

echo
echo "==> Construyendo ${IMAGE}:${VERSION}"
t0=$SECONDS
# --pull: la base (python:3.12-slim) con los ultimos parches de seguridad.
docker build --pull "${TAGS[@]}" \
  --label "org.opencontainers.image.title=trajet-server" \
  --label "org.opencontainers.image.version=${VERSION}" \
  --label "org.opencontainers.image.revision=${REVISION}" \
  .
echo "    construida en $(( SECONDS - t0 )) s"

# ---------------------------------------------------------------------------
# Prueba de humo: nunca se publica una imagen que no arranca
# ---------------------------------------------------------------------------
# Sin red (no llama a PRIM ni al portal de IDFM) y con una BD vacia en el
# volumen anonimo de /data, que se borra con el contenedor. Sin --user: se
# comprueba que la imagen YA corre como 1000, que es lo que espera Umbrel.
HUMO="trajet-humo-$$"
limpiar () { docker rm -f -v "$HUMO" >/dev/null 2>&1 || true; }
trap limpiar EXIT

echo "==> Prueba de humo"
docker run -d --name "$HUMO" --network none \
  -e PRIM_API_KEY= -e TRAJET_COLLECT=0 -e TRAJET_MAP=0 \
  "${IMAGE}:${VERSION}" >/dev/null
PING=""
for _ in $(seq 1 30); do
  PING=$(docker exec "$HUMO" python -c \
    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/ping', timeout=2).read().decode())" \
    2>/dev/null || true)
  [ -n "$PING" ] && break
  [ "$(docker inspect -f '{{.State.Running}}' "$HUMO" 2>/dev/null)" = "true" ] || break
  sleep 1
done
if [ -z "$PING" ]; then
  echo "    la imagen no responde en /api/v1/ping. Ultimas lineas del log:" >&2
  docker logs --tail 40 "$HUMO" >&2 || true
  falla "prueba de humo fallida: no publico nada"
fi
case "$PING" in
  *"\"version\":\"${CODE_VERSION}\""*) ;;
  *) falla "la imagen responde pero no anuncia la ${CODE_VERSION}: $PING" ;;
esac
UID_EN_MARCHA=$(docker exec "$HUMO" id -u)
[ "$UID_EN_MARCHA" = 1000 ] || falla "la imagen corre como uid ${UID_EN_MARCHA} y tiene que ser 1000"
echo "    responde (${PING}) y corre como uid 1000"
limpiar

# ---------------------------------------------------------------------------
# Publicar
# ---------------------------------------------------------------------------
echo "==> Publicando en ${REGISTRY}"
docker push -q "${IMAGE}:${VERSION}"
[ "$LATEST" = 1 ] && docker push -q "${IMAGE}:latest"
ya_publicada "$VERSION" || falla "el registro no tiene ${REPO_NAME}:${VERSION} despues del push"

echo
echo "==> Imagenes de Trajet en este Docker"
docker images --filter "reference=${IMAGE}" --format '    {{.Repository}}:{{.Tag}}  {{.Size}}'

# Lo que pide la tienda que ve este Umbrel (si esta en la ruta de siempre).
# Con sudo, HOME es el de root: se mira tambien el del usuario que lo lanzo.
TIENDA=""
for d in "${HOME}"/umbrel/app-stores/*/ismaeloul-trajet \
         "/home/${SUDO_USER:-umbrel}"/umbrel/app-stores/*/ismaeloul-trajet; do
  if [ -f "$d/docker-compose.yml" ]; then TIENDA="$d"; break; fi
done
PIDE=""
if [ -n "$TIENDA" ]; then
  PIDE=$(sed -n 's/^ *image: *\(.*ismaeloul-trajet\/trajet:[^ ]*\).*/\1/p' "$TIENDA/docker-compose.yml" | head -1)
fi

cat <<EOF

==> Hecho: ${IMAGE}:${VERSION} publicada.

Siguiente:
  1. La tienda tiene que pedir esta imagen: en umbrel-app-store,
     ismaeloul-trajet/docker-compose.yml con
       image: ${IMAGE}:${VERSION}
     y version "${VERSION}" en umbrel-app.yml, ya subido a GitHub.
  2. En Umbrel: App Store -> Trajet -> Actualizar (o Instalar si aun no la tienes).
  3. Abre Trajet desde Umbrel: en el panel pega la clave de PRIM, revisa las
     direcciones del QR y empareja el iPhone.

El registro local tiene que seguir en marcha: Umbrel descarga de ahi cada vez
que instala, actualiza o recrea la app.
EOF
if [ -n "$PIDE" ] && [ "$PIDE" != "${IMAGE}:${VERSION}" ]; then
  cat <<EOF

OJO: la tienda que ve este Umbrel todavia pide ${PIDE}
     (${TIENDA}). Falta subir la tienda o que Umbrel la refresque.
EOF
fi
