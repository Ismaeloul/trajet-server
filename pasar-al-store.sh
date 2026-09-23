#!/usr/bin/env bash
# Pasa Trajet del stack de pruebas a la app instalada desde el store de Umbrel.
#
#   bash ~/trajet/pasar-al-store.sh
#
# Antes hay que haber hecho el push del repo del store: la version 0.3.0 tiene
# que estar en GitHub. El script lo comprueba y no toca nada si no esta.
#
# Que hace, en orden:
#   1. Refresca el store cacheado y verifica que ve Trajet 0.3.0
#   2. Baja el stack de pruebas, que ocupa el 7796 (el puerto de la app)
#   3. Instala la app desde el store
#   4. Le pasa el fichero de configuracion y la base de datos aprendida
#      (302 andenes, ya sin las rutas de prueba)
#   5. Comprueba que arranca y que ve su configuracion
set -euo pipefail

STORE=~/umbrel/app-stores/ismaeloul-umbrel-app-store-github-ee478309
APP=~/umbrel/app-data/ismaeloul-trajet
ID=ismaeloul-trajet

echo "==> 1/5  Refrescando el store"
git -C "$STORE" pull --ff-only
VER=$(grep -E '^version:' "$STORE/$ID/umbrel-app.yml" 2>/dev/null | tr -d '"' | awk '{print $2}' || true)
if [ "$VER" != "0.3.0" ]; then
  echo
  echo "ERROR: el store no ve Trajet 0.3.0 (ve: '${VER:-nada}')."
  echo "       Falta el push. En el PC, dentro de umbrel-app-store:"
  echo "         git push origin main"
  echo "       Y vuelve a lanzar este script. No he tocado nada."
  exit 1
fi
echo "    ok, el store ve Trajet $VER"

echo "==> 2/5  Bajando el stack de pruebas (libera el 7796)"
(cd ~/trajet && docker compose down) || true

echo "==> 3/5  Instalando la app"
if [ -d "$APP" ]; then
  echo "    ya estaba instalada, la actualizo"
  umbreld client apps.update.mutate --appId "$ID" || true
else
  umbreld client apps.install.mutate --appId "$ID"
fi
for _ in $(seq 1 60); do
  [ -d "$APP" ] && break
  sleep 2
done
[ -d "$APP" ] || { echo "ERROR: $APP no aparece. Mira la UI de Umbrel."; exit 1; }

echo "==> 4/5  Configuracion y base de datos"
umbreld client apps.stop.mutate --appId "$ID" >/dev/null 2>&1 || true
sleep 3

# El .env del stack de pruebas, tal cual, sin pasar por pantalla ni por
# ninguna variable: docker compose lo lee del directorio del proyecto.
cp ~/trajet/.env "$APP/.env"
chmod 600 "$APP/.env"
echo "    $APP/.env puesto"

mkdir -p "$APP/data"
if [ -s "$APP/data/trajet.db" ]; then
  cp -a "$APP/data/trajet.db" "$APP/data/trajet.db.antes-de-migrar"
  echo "    habia una BD, guardada como trajet.db.antes-de-migrar"
fi
cp -a ~/trajet/data/trajet.db "$APP/data/trajet.db"
echo "    base de datos puesta:"
python3 - "$APP/data/trajet.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
for t in ("routes", "platform_obs", "translations"):
    print(f"      {t:14}", c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
PY

umbreld client apps.start.mutate --appId "$ID" >/dev/null

echo "==> 5/5  Comprobando"
SALUD=""
for _ in $(seq 1 45); do
  SALUD=$(curl -fsS -m 3 http://127.0.0.1:7796/api/health 2>/dev/null || true)
  [ -n "$SALUD" ] && break
  sleep 2
done

if [ -z "$SALUD" ]; then
  echo "    la app no responde en el 7796 todavia. Mirala en la UI de Umbrel:"
  echo "      umbreld client apps.logs.query --appId $ID | tail -40"
  exit 1
fi

CONF=$(printf '%s' "$SALUD" | python3 -c 'import json,sys; print(json.load(sys.stdin)["key_configured"])')
echo "    key_configured = $CONF"

echo
if [ "$CONF" != "True" ]; then
  cat <<'AVISO'
    OJO: umbreld no ha interpolado el .env, asi que la app esta instalada
    pero no puede consultar nada. El plan B es poner la clave directamente
    en la linea PRIM_API_KEY de:
        ~/umbrel/app-data/ismaeloul-trajet/docker-compose.yml
    y luego:
        umbreld client apps.restart.mutate --appId ismaeloul-trajet
    (eso se pierde al actualizar la app; si pasa, avisame y lo resuelvo bien)
AVISO
else
  echo "    Listo: Trajet instalado desde el store, en el 7796."
fi
echo
echo "El stack de pruebas se queda en ~/trajet por si acaso, con la BD original"
echo "en data/trajet.db.bak-2026-08-30. Cuando lo veas bien: rm -rf ~/trajet"
