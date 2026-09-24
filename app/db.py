"""Persistencia en SQLite: rutas, tramos e historial."""
import json
import math
import os
import sqlite3
from contextlib import contextmanager

from .config import settings

# El esquema vive en app/migrations (versionado con PRAGMA user_version).
# Aqui solo quedan la conexion y el acceso a datos.

# Columnas de la v2 que no forman parte del contrato de la 0.3.0: se leen
# aparte (el mapa las usa) para que /api/routes siga devolviendo lo mismo.
_LEG_V2_COLUMNS = ("from_lat", "from_lon", "to_lat", "to_lon")

# Motivo por el que init() fallo al arrancar, o None. Lo pone app/main.py:
# con la BD a medio migrar el servidor arranca igual, en modo degradado (las
# APIs de datos responden 503 y el panel ensena el motivo), en vez de caerse
# y entrar en un bucle de reinicios en el que nadie ve que ha pasado.
init_error: str | None = None


def _connect() -> sqlite3.Connection:
    path = settings.db_path
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 10000")
    return con


@contextmanager
def conn():
    con = _connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> list[int]:
    """Crea o migra la BD. Devuelve las migraciones aplicadas.

    Va con su propia conexion en modo autocommit para que cada migracion
    controle su transaccion (BEGIN IMMEDIATE ... COMMIT).
    """
    from .migrations import migrate

    path = settings.db_path
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode = WAL")
        return migrate(con, path)
    finally:
        con.close()


def schema_version() -> int:
    with conn() as c:
        return int(c.execute("PRAGMA user_version").fetchone()[0])


def _leg_dict(row: sqlite3.Row, with_coords: bool = False) -> dict:
    d = dict(row)
    if not with_coords:
        for col in _LEG_V2_COLUMNS:
            d.pop(col, None)
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


def get_route(route_id: int, with_coords: bool = False) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM routes WHERE id = ?", (route_id,)).fetchone()
        if not r:
            return None
        legs = c.execute(
            "SELECT * FROM legs WHERE route_id = ? ORDER BY seq",
            (route_id,)).fetchall()
    return _route_dict(r, [_leg_dict(leg, with_coords) for leg in legs])


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
                " line_color, from_id, from_name, to_id, to_name, directions, "
                " from_lat, from_lon, to_lat, to_lon) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (route_id, i,
                 leg["line_id"], leg.get("line_code", ""),
                 leg.get("line_name", ""), leg.get("line_mode", ""),
                 leg.get("line_color", ""),
                 leg["from_id"], leg.get("from_name", ""),
                 leg.get("to_id", ""), leg.get("to_name", ""),
                 json.dumps(leg.get("directions", []), ensure_ascii=False),
                 _coord(leg.get("from_lat")), _coord(leg.get("from_lon")),
                 _coord(leg.get("to_lat")), _coord(leg.get("to_lon"))))
    return int(route_id)


def _coord(value) -> float | None:
    try:
        v = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    # NaN o infinito no son una coordenada: el mapa se liaria con ellos.
    return v if v is not None and math.isfinite(v) else None


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
    """Resumen para la pantalla de estadisticas.

    El corte se calcula en hora de Paris, igual que la columna `day` (en la
    0.3.0 se comparaba con la fecha UTC de SQLite). `by_line[].n` son
    observaciones de historial, no dias.
    """
    from datetime import datetime, timedelta

    days_back = max(1, min(int(days_back), 3650))
    desde = (datetime.now(settings.tz).date()
             - timedelta(days=days_back)).isoformat()
    with conn() as c:
        by_month = c.execute(
            "SELECT substr(day,1,7) AS month, "
            "       COUNT(DISTINCT CASE WHEN disrupted > 0 THEN day END) AS bad_days, "
            "       COUNT(DISTINCT day) AS total_days, "
            "       AVG(delay_min) AS avg_delay "
            "FROM history WHERE day >= ? "
            "GROUP BY month ORDER BY month DESC",
            (desde,)).fetchall()
        by_line = c.execute(
            "SELECT worst_line, COUNT(*) AS n, AVG(delay_min) AS avg_delay "
            "FROM history WHERE worst_line != '' AND disrupted > 0 "
            "  AND day >= ? "
            "GROUP BY worst_line ORDER BY n DESC LIMIT 10",
            (desde,)).fetchall()
        overall = c.execute(
            "SELECT COUNT(*) AS n, AVG(delay_min) AS avg_delay, "
            "       MAX(delay_min) AS max_delay "
            "FROM history WHERE day >= ?",
            (desde,)).fetchone()
    return {
        "by_month": [dict(r) for r in by_month],
        "by_line": [dict(r) for r in by_line],
        "overall": dict(overall) if overall else {},
    }


# ---------------- ajustes del panel ----------------

def get_setting(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        r = c.execute("SELECT v FROM settings_kv WHERE k = ?", (key,)).fetchone()
    return r["v"] if r else default


def set_setting(key: str, value: str | None) -> None:
    with conn() as c:
        if value is None:
            c.execute("DELETE FROM settings_kv WHERE k = ?", (key,))
        else:
            c.execute(
                "INSERT INTO settings_kv (k, v, updated_at) "
                "VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v, "
                "updated_at = excluded.updated_at", (key, value))


def counts() -> dict:
    """Tamanos para el panel."""
    with conn() as c:
        out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("routes", "history", "platform_obs")}
    try:
        out["size_bytes"] = os.path.getsize(settings.db_path)
    except OSError:
        out["size_bytes"] = 0
    return out
