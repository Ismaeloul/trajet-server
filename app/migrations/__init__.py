"""Migraciones versionadas de SQLite.

La version del esquema se guarda en `PRAGMA user_version`. Una BD de la
0.3.0 tiene version 0 y sus tablas ya creadas: la migracion 1 es idempotente
y la deja en 1 sin tocar ni un dato.

Reglas:
  - Cada migracion va en su propia transaccion. Si falla, rollback y la
    version no avanza: nunca queda un esquema a medias.
  - Antes de migrar una BD que ya tiene datos se copia al lado
    (`trajet.db.bak-v<N>`), una vez por version, sin pisar copias viejas.
  - Mis rutas, el historial y los andenes aprendidos NO se borran nunca en
    una migracion (hay tests que lo comprueban contra la 0.3.0).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Callable

from . import m0001_base, m0002_v2

log = logging.getLogger("trajet.migrations")

# (version, descripcion, funcion). La funcion recibe la conexion ya dentro
# de la transaccion y no hace commit.
MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, m0001_base.DESCRIPTION, m0001_base.apply),
    (2, m0002_v2.DESCRIPTION, m0002_v2.apply),
]
LATEST = MIGRATIONS[-1][0]


def current_version(con: sqlite3.Connection) -> int:
    return int(con.execute("PRAGMA user_version").fetchone()[0])


def _has_user_data(con: sqlite3.Connection) -> bool:
    tablas = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("routes", "history", "platform_obs"):
        if t in tablas and con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():
            return True
    return False


def _backup(con: sqlite3.Connection, db_path: str, version: int) -> str | None:
    """Copia consistente de la BD antes de migrar (API de backup de SQLite)."""
    if not db_path or db_path == ":memory:":
        return None
    dest = f"{db_path}.bak-v{version}"
    if os.path.exists(dest):
        return dest
    target = sqlite3.connect(dest)
    try:
        con.backup(target)
    finally:
        target.close()
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    log.info("copia de seguridad antes de migrar: %s", dest)
    return dest


def migrate(con: sqlite3.Connection, db_path: str = "") -> list[int]:
    """Aplica en orden las migraciones pendientes. Devuelve las aplicadas."""
    aplicadas: list[int] = []
    version = current_version(con)
    pendientes = [m for m in MIGRATIONS if m[0] > version]
    if not pendientes:
        return aplicadas
    if _has_user_data(con):
        _backup(con, db_path, version)

    # PRAGMA foreign_keys no se puede cambiar dentro de una transaccion.
    con.execute("PRAGMA foreign_keys = ON")
    for numero, descripcion, funcion in pendientes:
        try:
            con.execute("BEGIN IMMEDIATE")
            funcion(con)
            con.execute(f"PRAGMA user_version = {int(numero)}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            log.exception("fallo la migracion %d (%s)", numero, descripcion)
            raise
        log.info("migracion %d aplicada: %s", numero, descripcion)
        aplicadas.append(numero)
    return aplicadas
