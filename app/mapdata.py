"""Datos del mapa (datos abiertos de IDFM).

ESQUELETO de la base de la FASE 1: lo implementa el modulo de mapa siguiendo
docs/datos-idfm.md §8 y el esquema RouteMap de docs/openapi.yaml.
"""
from datetime import datetime, timezone


async def startup() -> None:
    return None


async def shutdown() -> None:
    return None


async def route_map(route: dict) -> dict:
    return {
        "route_id": route["id"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pending": True, "stale": False,
        "lines": [], "stations": [], "accesses": [], "transfers": [],
        "sources": {}, "license": "",
    }
