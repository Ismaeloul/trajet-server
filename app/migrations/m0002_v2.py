"""Migracion 2: lo nuevo de la v2.

Solo AÑADE: tablas nuevas y columnas opcionales. No toca ni borra ninguna fila
de rutas, tramos, historial ni andenes.
"""
import sqlite3

from .m0001_base import add_missing_columns

DESCRIPTION = ("v2: dispositivos, emparejamiento, cuota, errores, ajustes, "
               "cache del mapa, puntuacion por tren y coordenadas de tramos")

TABLES = [
    # Dispositivos emparejados. Del token solo se guarda su SHA-256.
    """CREATE TABLE IF NOT EXISTS devices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    model        TEXT    NOT NULL DEFAULT '',
    app_version  TEXT    NOT NULL DEFAULT '',
    token_hash   TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    last_ip      TEXT,
    revoked_at   TEXT
)""",
    # Codigos de emparejamiento de un solo uso. `id` es aleatorio y es lo que
    # ve el panel; del codigo en si solo se guarda su SHA-256.
    """CREATE TABLE IF NOT EXISTS pairing_codes (
    id           TEXT    PRIMARY KEY,
    code_hash    TEXT    NOT NULL UNIQUE,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    used_at      TEXT,
    cancelled_at TEXT,
    device_id    INTEGER REFERENCES devices(id) ON DELETE SET NULL
)""",
    # Llamadas a PRIM por dia UTC, endpoint y clave (key_id = hash corto de
    # la clave: al cambiarla, el contador empieza de cero).
    """CREATE TABLE IF NOT EXISTS quota_usage (
    day                TEXT    NOT NULL,
    endpoint           TEXT    NOT NULL,
    key_id             TEXT    NOT NULL DEFAULT '',
    used               INTEGER NOT NULL DEFAULT 0,
    remaining_reported INTEGER,
    updated_at         TEXT    NOT NULL,
    PRIMARY KEY (day, endpoint, key_id)
)""",
    # Errores recientes para el panel (ya sin secretos).
    """CREATE TABLE IF NOT EXISTS error_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    logger  TEXT NOT NULL,
    message TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_error_ts ON error_log(ts)",
    # Ajustes que se cambian desde el panel (direcciones del QR, nombre).
    """CREATE TABLE IF NOT EXISTS settings_kv (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    # Mapa: resultados ya procesados de los datos abiertos de IDFM.
    """CREATE TABLE IF NOT EXISTS map_cache (
    key            TEXT PRIMARY KEY,
    body           BLOB NOT NULL,
    raw_hash       TEXT NOT NULL DEFAULT '',
    fetched_at     TEXT NOT NULL,
    source_version TEXT NOT NULL DEFAULT ''
)""",
    # Acierto de la prevision contado POR TREN (la tabla de la 0.3.0 contaba
    # combinaciones prevision/realidad y quedaba sesgada; se conserva tal
    # cual como historico).
    """CREATE TABLE IF NOT EXISTS platform_score_v2 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL,
    stop_id     TEXT    NOT NULL,
    line_id     TEXT    NOT NULL,
    train_key   TEXT    NOT NULL,
    predicted   TEXT    NOT NULL,
    actual      TEXT    NOT NULL,
    hit         INTEGER NOT NULL,
    basis       TEXT    NOT NULL DEFAULT ''
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_score_v2_tren
    ON platform_score_v2(day, stop_id, line_id, train_key)""",
]

# Coordenadas de subida y bajada de cada tramo, cuando se conocen (el
# planificador las trae de Navitia). Sirven al mapa como ultimo recurso.
COLUMNS = [
    ("legs", "from_lat", "REAL"),
    ("legs", "from_lon", "REAL"),
    ("legs", "to_lat", "REAL"),
    ("legs", "to_lon", "REAL"),
]


def apply(con: sqlite3.Connection) -> None:
    for sql in TABLES:
        con.execute(sql)
    add_missing_columns(con, COLUMNS)
