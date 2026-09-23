#!/usr/bin/env bash
# Pruebas contra la API REAL de PRIM (tests/real). Gastan cuota de verdad:
# unas 16 llamadas por pasada, con un tope de 800 por endpoint y dia UTC que
# se lleva en .local/real-quota-<dia>.json (la cuota real es de 1000 y el
# servidor de casa tambien la usa).
#
#   bash scripts/test-real.sh            # desde trajet-server
#   bash scripts/test-real.sh -k places  # se le pueden pasar opciones de pytest
#
# La clave sale de PRIM_API_KEY o de .env. Este script no la imprime nunca.
set -euo pipefail
cd "$(dirname "$0")/.."

# .env con lineas CLAVE=valor. Se lee a mano en vez de con `source` para no
# ejecutar nada que haya dentro; lo que ya este en el entorno manda.
if [ -f .env ]; then
  while IFS= read -r linea || [ -n "$linea" ]; do
    linea="${linea#export }"
    [[ "$linea" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || continue
    nombre="${linea%%=*}"
    valor="${linea#*=}"
    valor="${valor%\"}"; valor="${valor#\"}"
    valor="${valor%\'}"; valor="${valor#\'}"
    if [ -z "${!nombre:-}" ]; then
      export "$nombre=$valor"
    fi
  done < .env
fi

export TRAJET_QUOTA_CAP=800

PY=python
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
fi

# -p no:cacheprovider: nada de .pytest_cache con restos de estas pruebas.
exec "$PY" -m pytest tests/real -m real -p no:cacheprovider "$@"
