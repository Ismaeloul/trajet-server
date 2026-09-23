"""Persistencia en SQLite: rutas, tramos e historial."""
import json
import os
import sqlite3
from contextlib import contextmanager

from .config import settings

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS routes (
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
);

CREATE TABLE IF NOT EXISTS legs (
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
);
CREATE INDEX IF NOT EXISTS idx_legs_route ON legs(route_id, seq);

CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id    INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    ts          TEXT    NOT NULL,
    day         TEXT    NOT NULL,
    disrupted   INTEGER NOT NULL DEFAULT 0,
    delay_min   REAL    NOT NULL DEFAULT 0,
    worst_line  TEXT    NOT NULL DEFAULT '',
    detail      TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_hist_route_day ON history(route_id, day);
CREATE INDEX IF NOT EXISTS idx_hist_ts ON history(ts);

-- Cada vez que se ve un anden de verdad se apunta aqui, para poder predecir
-- de que via saldra manana el mismo tren. Solo se recogen las estaciones de
-- MIS rutas: no tiene sentido estudiar toda la red.
--
-- Una fila por tren y por dia: el tablero se refresca cada 30 s y sin esa
-- restriccion un solo tren contaria 40 veces y falsearia el porcentaje.
CREATE TABLE IF NOT EXISTS platform_obs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL,           -- YYYY-MM-DD, hora de Paris
    seen_at     TEXT    NOT NULL,
    stop_id     TEXT    NOT NULL,
    line_id     TEXT    NOT NULL,
    dest        TEXT    NOT NULL,           -- destino normalizado
    train       TEXT    NOT NULL DEFAULT '',-- numero de mision, si lo hay
    aimed       TEXT    NOT NULL DEFAULT '',-- hora teorica HH:MM
    weekday     INTEGER NOT NULL,           -- 0=lunes .. 6=domingo
    platform    TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_unico
    ON platform_obs(day, stop_id, line_id, train, aimed, dest);
CREATE INDEX IF NOT EXISTS idx_obs_tren  ON platform_obs(stop_id, line_id, train);
CREATE INDEX IF NOT EXISTS idx_obs_hora  ON platform_obs(stop_id, line_id, dest, aimed);

-- Aciertos y fallos de la prediccion, para poder decir si sirve de algo.
CREATE TABLE IF NOT EXISTS platform_score (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT    NOT NULL,
    stop_id     TEXT    NOT NULL,
    line_id     TEXT    NOT NULL,
    predicted   TEXT    NOT NULL,
    actual      TEXT    NOT NULL,
    hit         INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_score_unico
    ON platform_score(day, stop_id, line_id, predicted, actual);

-- Avisos ya traducidos. Se guarda el frances original al lado, para poder
-- comprobar la traduccion, y el modelo que la hizo.
CREATE TABLE IF NOT EXISTS translations (
    k       TEXT PRIMARY KEY,      -- hash del texto original
    fr      TEXT NOT NULL,
    es      TEXT NOT NULL,
    model   TEXT NOT NULL DEFAULT '',
    ts      TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    path = settings.db_path
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def conn():
    con = _connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


# Columnas anadidas despues de la primera version. CREATE TABLE IF NOT EXISTS
# no toca una tabla que ya existe, asi que hay que anadirlas a mano. Es
# idempotente: se comprueba antes si estan.
MIGRACIONES = [
    ("routes", "time_mode", "TEXT NOT NULL DEFAULT 'window'"),
    ("routes", "time_at", "TEXT NOT NULL DEFAULT ''"),
    ("routes", "duration_min", "INTEGER NOT NULL DEFAULT 0"),
]


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for tabla, columna, tipo in MIGRACIONES:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tabla})")}
            if columna not in cols:
                c.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")


def _leg_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["directions"] = json.loads(d["directions"])
    except (ValueError, TypeError):
        d["directions"] = []
    return d


def _route_dict(row: sqlite3.Row, legs: list) -> dict:
    d = dict(row)
    d["days"] = [int(x) for x in d["days"].split(",") if x.strip() != ""]
    d["legs"] = legs
    return d


def list_routes() -> list[dict]:
    with conn() as c:
        routes = c.execute("SELECT * FROM routes ORDER BY position, id").fetchall()
        legs = c.execute("SELECT * FROM legs ORDER BY route_id, seq").fetchall()
    by_route: dict[int, list] = {}
    for leg in legs:
        by_route.setdefault(leg["route_id"], []).append(_leg_dict(leg))
    return [_route_dict(r, by_route.get(r["id"], [])) for r in routes]


def get_route(route_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not r:
            return None
        legs = c.execute(
            "SELECT * FROM legs WHERE route_id = ? ORDER BY seq",
            (route_id,)).fetchall()
    return _route_dict(r, [_leg_dict(leg) for leg in legs])


def save_route(data: dict, route_id: int | None = None) -> int:
    from .board import derive_window

    days = ",".join(str(int(d)) for d in data.get("days", [0, 1, 2, 3, 4]))
    mode = data.get("time_mode") or "window"
    if mode not in ("window", "departure", "arrival"):
        mode = "window"
    at = (data.get("time_at") or "").strip()
    duracion = int(data.get("duration_min") or 0)

    # La franja se guarda siempre, aunque la ruta se defina por hora de salida
    # o de llegada: es lo que consulta todo lo demas.
    desde, hasta = derive_window({
        "time_mode": mode, "time_at": at, "duration_min": duracion,
        "time_from": data.get("time_from", "07:00"),
        "time_to": data.get("time_to", "10:00"),
    })

    fields = (
        data["name"].strip(),
        data["origin_id"], data["origin_name"],
        data["dest_id"], data["dest_name"],
        days, desde, hasta,
        mode, at, duracion,
        int(data.get("position", 0)),
    )
    with conn() as c:
        if route_id is None:
            cur = c.execute(
                "INSERT INTO routes "
                "(name, origin_id, origin_name, dest_id, dest_name, "
                " days, time_from, time_to, time_mode, time_at, "
                " duration_min, position) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", fields)
            route_id = int(cur.lastrowid)
        else:
            c.execute(
                "UPDATE routes SET name=?, origin_id=?, origin_name=?, "
                "dest_id=?, dest_name=?, days=?, time_from=?, time_to=?, "
                "time_mode=?, time_at=?, duration_min=?, "
                "position=? WHERE id=?", fields + (route_id,))
            c.execute("DELETE FROM legs WHERE route_id = ?", (route_id,))

        for i, leg in enumerate(data.get("legs", [])):
            c.execute(
                "INSERT INTO legs "
                "(route_id, seq, line_id, line_code, line_name, line_mode, "
                " line_color, from_id, from_name, to_id, to_name, directions) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (route_id, i,
                 leg["line_id"], leg.get("line_code", ""),
                 leg.get("line_name", ""), leg.get("line_mode", ""),
                 leg.get("line_color", ""),
                 leg["from_id"], leg.get("from_name", ""),
                 leg.get("to_id", ""), leg.get("to_name", ""),
                 json.dumps(leg.get("directions", []), ensure_ascii=False)))
    return int(route_id)


def delete_route(route_id: int) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        return cur.rowcount > 0


def log_observation(route_id: int, ts_iso: str, day: str, disrupted: int,
                    delay_min: float, worst_line: str, detail: dict) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO history "
            "(route_id, ts, day, disrupted, delay_min, worst_line, detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (route_id, ts_iso, day, disrupted, delay_min, worst_line,
             json.dumps(detail, ensure_ascii=False)))


def route_usage(days: int = 14) -> dict[int, int]:
    """Cuantas veces se ha consultado cada ruta ultimamente.

    Sirve para decidir que estaciones merece la pena estudiar: una ruta que se
    probo una vez y no se ha vuelto a mirar no deberia gastar cuota a diario.
    """
    with conn() as c:
        filas = c.execute(
            "SELECT route_id, COUNT(*) n FROM history "
            "WHERE day >= date('now', ?) GROUP BY route_id",
            (f"-{int(days)} days",)).fetchall()
    return {r["route_id"]: r["n"] for r in filas}


def last_observation_ts(route_id: int) -> str | None:
    with conn() as c:
        r = c.execute("SELECT MAX(ts) AS t FROM history WHERE route_id = ?",
                      (route_id,)).fetchone()
    return r["t"] if r else None


def stats(days_back: int = 90) -> dict:
    """Resumen para la pantalla del PASO 3."""
    with conn() as c:
        by_month = c.execute(
            "SELECT substr(day,1,7) AS month, "
            "       COUNT(DISTINCT CASE WHEN disrupted > 0 THEN day END) AS bad_days, "
            "       COUNT(DISTINCT day) AS total_days, "
            "       AVG(delay_min) AS avg_delay "
            "FROM history WHERE day >= date('now', ?) "
            "GROUP BY month ORDER BY month DESC",
            (f"-{days_back} days",)).fetchall()
        by_line = c.execute(
            "SELECT worst_line, COUNT(*) AS n, AVG(delay_min) AS avg_delay "
            "FROM history WHERE worst_line != '' AND disrupted > 0 "
            "  AND day >= date('now', ?) "
            "GROUP BY worst_line ORDER BY n DESC LIMIT 10",
            (f"-{days_back} days",)).fetchall()
        overall = c.execute(
            "SELECT COUNT(*) AS n, AVG(delay_min) AS avg_delay, "
            "       MAX(delay_min) AS max_delay "
            "FROM history WHERE day >= date('now', ?)",
            (f"-{days_back} days",)).fetchone()
    return {
        "by_month": [dict(r) for r in by_month],
        "by_line": [dict(r) for r in by_line],
        "overall": dict(overall) if overall else {},
    }
