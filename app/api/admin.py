"""API del panel (/api/admin/*), detras del login de Umbrel.

ESQUELETO de la base de la FASE 1: lo implementa el modulo del panel
siguiendo docs/openapi.yaml.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/admin")
