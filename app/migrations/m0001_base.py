"""Migracion 1: el esquema de la 0.3.0, tal cual.

Es idempotente: sobre una BD vacia la crea, y sobre una BD de produccion 0.3.0
(que ya tiene todo) no cambia nada salvo anadir las columnas que falten, igual
que hacia `db.init()` en la 0.3.0. No se ejecuta PRAGMA journal_mode aqui
(no se puede dentro de una transaccion): lo pone db.py al conectar.
"""
import sqlite3

DESCRIPTION = "esquema base de la 0.3.0 (rutas, tramos, historial, andenes, traducciones)"

TABLES = [
    """CREATE TABLE IF NOT EXISTS routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    origin_id   TEXT    NOT NULL,
    origin_name TEXT    NOT NULL,
    dest_id     TEXT    NOT NULL,
    dest_name   TEXT    NOT NULL,
    -- Dias de la semana en que uso la ruta: 0=lunes .. 6=domingo
    days        TEXT    NOT NULL DEFAULT '0,1,2,3,4',
    -- Franja horaria habitual, hora local de Paris. Se calcula sola cuando
    -- la ruta se define por hora de salida o de llegada.
    time_from   TEXT    NOT NULL DEFAULT '07:00',
    time_to     TEXT    NOT NULL DEFAULT '10:00',
    -- Como quiero pensar el horario: 'window' (de X a Y), 'departure'
    -- (salgo a las X) o 'arrival' (quiero llegar a las X).
    time_mode   TEXT    NOT NULL DEFAULT 'window',
    time_at     TEXT    NOT NULL DEFAULT '',
    -- Duracion del trayecto en minutos, si la sabemos por el planificador.
    duration_min INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
)""",
    """CREATE TABLE IF NOT EXISTS legs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id    INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    line_id     TEXT    NOT NULL,
    line_code   TEXT    NOT NULL,
    line_name   TEXT    NOT NULL DEFAULT '',
    line_mode   TEXT    NOT NULL DEFAULT '',
    line_color  TEXT    NOT NULL DEFAULT '',
    from_id     TEXT    NOT NULL,
    from_name   TEXT    NOT NULL,
    to_id       TEXT    NOT NULL DEFAULT '',
    to_name     TEXT    NOT NULL DEFAULT '',
    -- Destinos que cuentan como mi direccion, en JSON. Vacio = no filtrar.
    directions  TEXT    NOT NULL DEFAULT '[]'
)""",
    "CREATE INDEX IF NOT EXISTS idx_legs_route ON legs(route_id, seq)",
    """CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id    INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    ts          TEXT    NOT NULL,
    day         TEXT    NOT NULL,
    disrupted   INTEGER NOT NULL DEFAULT 0,
    delay_min   REAL    NOT NULL DEFAULT 0,
    worst_line  TEXT    NOT NULL DEFAULT '',
    detail      TEXT    NOT NULL DEFAULT '{}'
)""",
    "CREATE INDEX IF NOT EXISTS idx_hist_route_day ON history(route_id, day)",
    "CREATE INDEX IF NOT EXISTS idx_hist_ts ON history(ts)",
    # Una fila por tren y por dia: el tablero se refresca cada 30 s y sin esa
    # restriccion un solo tren contaria 40 veces y falsearia el porcentaje.
    """CREATE TABLE IF NOT EXISTS platform_obs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL,
    seen_at     TEXT    NOT NULL,
    stop_id     TEXT    NOT NULL,
    line_id     TEXT    NOT NULL,
    dest        TEXT    NOT NULL,
    train       TEXT    NOT NULL DEFAULT '',
    aimed       TEXT    NOT NULL DEFAULT '',
    weekday     INTEGER NOT NULL,
    platform    TEXT    NOT NULL
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_unico
    ON platform_obs(day, stop_id, line_id, train, aimed, dest)""",
    "CREATE INDEX IF NOT EXISTS idx_obs_tren  ON platform_obs(stop_id, line_id, train)",
    "CREATE INDEX IF NOT EXISTS idx_obs_hora  ON platform_obs(stop_id, line_id, dest, aimed)",
    """CREATE TABLE IF NOT EXISTS platform_score (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL,
    stop_id     TEXT    NOT NULL,
    line_id     TEXT    NOT NULL,
    predicted   TEXT    NOT NULL,
    actual      TEXT    NOT NULL,
    hit         INTEGER NOT NULL
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_score_unico
    ON platform_score(day, stop_id, line_id, predicted, actual)""",
    """CREATE TABLE IF NOT EXISTS translations (
    k       TEXT PRIMARY KEY,
    fr      TEXT NOT NULL,
    es      TEXT NOT NULL,
    model   TEXT NOT NULL DEFAULT '',
    ts      TEXT NOT NULL
)""",
]

# Columnas anadidas en la 0.2/0.3 (las mismas que MIGRACIONES en db.py 0.3.0).
COLUMNS = [
    ("routes", "time_mode", "TEXT NOT NULL DEFAULT 'window'"),
    ("routes", "time_at", "TEXT NOT NULL DEFAULT ''"),
    ("routes", "duration_min", "INTEGER NOT NULL DEFAULT 0"),
]


def add_missing_columns(con: sqlite3.Connection, columns) -> None:
    for tabla, columna, tipo in columns:
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({tabla})")}
        if columna not in cols:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")


def apply(con: sqlite3.Connection) -> None:
    for sql in TABLES:
        con.execute(sql)
    add_missing_columns(con, COLUMNS)
