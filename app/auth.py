"""Emparejamiento, dispositivos y tokens.

ESQUELETO de la base de la FASE 1: lo implementa el modulo de seguridad
siguiendo docs/servidor-v2.md («auth») y docs/openapi.yaml.
"""
from fastapi import Request

from .api.errors import ApiError


async def require_device(request: Request) -> dict:
    """Dependencia de FastAPI para /api/v1: devuelve el dispositivo o 401."""
    raise ApiError("unauthorized", "falta el token del dispositivo")


async def require_panel(request: Request) -> None:
    """Dependencia para /api/admin: origen de la conexion y cabecera anti-CSRF."""
    return None
