#!/usr/bin/env bash
# Copia el contrato de la v2 desde trajet-ios (la fuente) a los tests del
# servidor. Se lanza desde trajet-server con trajet-ios al lado:
#   bash scripts/sync-contract.sh ../docs
set -euo pipefail
SRC="${1:-../docs}"
cp "$SRC/openapi.yaml" tests/contract/openapi.yaml
echo "contrato copiado de $SRC/openapi.yaml"
