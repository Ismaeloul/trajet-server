"""Logs sin secretos y registro de errores recientes para el panel.

ESQUELETO de la base de la FASE 1: lo completa el modulo de seguridad
(filtro que tacha la clave PRIM, tokens y codigos; tabla error_log).
"""
import logging

from .config import settings


def setup() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def recent_errors(limit: int = 50) -> list[dict]:
    return []


def register_secret(value: str | None) -> None:
    """Apunta un secreto (p. ej. la clave PRIM en uso) para tacharlo de
    cualquier log. Lo llama prim.py al arrancar y al cambiar de clave."""
    return None
