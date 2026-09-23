"""Migraciones: una BD de la 0.3.0 pasa a la v2 sin perder nada (R89).

La BD sintetica se crea con el esquema EXACTO de la 0.3.0 (copiado de
app/db.py de produccion) y datos con la forma real. Si existe la copia real de
produccion en trajet-server/.local/trajet-prod.db (nunca en git), el ultimo
test la migra tambien — sobre una copia, jamas sobre el original.
"""
from __future__ import annotations

import os
import shutil
import sqlite3

import pytest

# Esquema de la 0.3.0 tal cual estaba en app/db.py (con la PRAGMA de WAL).
SCHEMA_030 = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    origin_id TEXT NOT NULL, origin_name TEXT NOT NULL,
    dest_id TEXT NOT NULL, dest_name TEXT NOT NULL,
    days TEXT NOT NULL DEFAULT '0,1,2,3,4',
    time_from TEXT NOT NULL DEFAULT '07:00', time_to TEXT NOT NULL DEFAULT '10:00',
    time_mode TEXT NOT NULL DEFAULT 'window', time_at TEXT NOT NULL DEFAULT '',
    duration_min INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, line_id TEXT NOT NULL, line_code TEXT NOT NULL,
    line_name TEXT NOT NULL DEFAULT '', line_mode TEXT NOT NULL DEFAULT '',
    line_color TEXT NOT NULL DEFAULT '', from_id TEXT NOT NULL, from_name TEXT NOT NULL,
    to_id TEXT NOT NULL DEFAULT '', to_name TEXT NOT NULL DEFAULT '',
    directions TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS idx_legs_route ON legs(route_id, seq);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
    ts TEXT NOT NULL, day TEXT NOT NULL, disrupted INTEGER NOT NULL DEFAULT 0,
    delay_min REAL NOT NULL DEFAULT 0, worst_line TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_hist_route_day ON history(route_id, day);
CREATE INDEX IF NOT EXISTS idx_hist_ts ON history(ts);
CREATE TABLE IF NOT EXISTS platform_obs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, seen_at TEXT NOT NULL,
    stop_id TEXT NOT NULL, line_id TEXT NOT NULL, dest TEXT NOT NULL,
    train TEXT NOT NULL DEFAULT '', aimed TEXT NOT NULL DEFAULT '',
    weekday INTEGER NOT NULL, platform TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_obs_unico ON platform_obs(day, stop_id, line_id, train, aimed, dest);
CREATE INDEX IF NOT EXISTS idx_obs_tren ON platform_obs(stop_id, line_id, train);
CREATE INDEX IF NOT EXISTS idx_obs_hora ON platform_obs(stop_id, line_id, dest, aimed);
CREATE TABLE IF NOT EXISTS platform_score (
    id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, stop_id TEXT NOT NULL,
    line_id TEXT NOT NULL, predicted TEXT NOT NULL, actual TEXT NOT NULL, hit INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_score_unico ON platform_score(day, stop_id, line_id, predicted, actual);
CREATE TABLE IF NOT EXISTS translations (
    k TEXT PRIMARY KEY, fr TEXT NOT NULL, es TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL);
"""

# Esquema de la 0.1 (sin time_mode/time_at/duration_min) para probar que
# tambien se recoge una BD muy vieja.
SCHEMA_010_ROUTES = """
CREATE TABLE routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    origin_id TEXT NOT NULL, origin_name TEXT NOT NULL,
    dest_id TEXT NOT NULL, dest_name TEXT NOT NULL,
    days TEXT NOT NULL DEFAULT '0,1,2,3,4',
    time_from TEXT NOT NULL DEFAULT '07:00', time_to TEXT NOT NULL DEFAULT '10:00',
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _poblar(path: str) -> dict:
    """Mete datos con la forma real y devuelve lo que tiene que sobrevivir."""
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_030)
    con.execute(
        "INSERT INTO routes (name, origin_id, origin_name, dest_id, dest_name, days, "
        "time_from, time_to, time_mode, time_at, duration_min, position) VALUES "
        "('Casa → Trabajo','stop_area:IDFM:71370','Gare Saint-Lazare','stop_area:IDFM:65063',"
        "'Argenteuil','0,1,2,3,4','07:15','09:15','arrival','09:00',16,0)")
    con.execute(
        "INSERT INTO legs (route_id, seq, line_id, line_code, line_name, line_mode, line_color, "
        "from_id, from_name, to_id, to_name, directions) VALUES "
        "(1,0,'line:IDFM:C01739','J','J','Train','CEC73D','stop_area:IDFM:71370',"
        "'Gare Saint-Lazare','stop_area:IDFM:65063','Argenteuil','[\"Ermont - Eaubonne\"]')")
    for i in range(12):
        con.execute(
            "INSERT INTO history (route_id, ts, day, disrupted, delay_min, worst_line, detail) "
            "VALUES (1, ?, ?, ?, ?, 'J', '{}')",
            (f"2026-08-{10 + i:02d}T07:30:00+00:00", f"2026-08-{10 + i:02d}", i % 3, float(i % 4)))
    for i in range(302):
        con.execute(
            "INSERT INTO platform_obs (day, seen_at, stop_id, line_id, dest, train, aimed, weekday, platform) "
            "VALUES (?, ?, 'stop_area:IDFM:71370', 'line:IDFM:C01739', 'ermont - eaubonne', ?, ?, ?, ?)",
            (f"2026-08-{1 + i % 28:02d}", "2026-08-01T07:00:00+02:00", str(135700 + i),
             f"{7 + i % 12:02d}:{i % 60:02d}", i % 7, str(20 + i % 5)))
    con.execute("INSERT INTO platform_score (day, stop_id, line_id, predicted, actual, hit) "
                "VALUES ('2026-08-20','stop_area:IDFM:71370','line:IDFM:C01739','21','21',1)")
    con.execute("INSERT INTO translations (k, fr, es, model, ts) VALUES "
                "('abc','Trafic interrompu','Tráfico interrumpido','gemma3:4b','2026-08-30')")
    con.commit()
    snap = _snapshot(con)
    con.close()
    return snap


def _snapshot(con: sqlite3.Connection) -> dict:
    out = {}
    for t in ("routes", "legs", "history", "platform_obs", "platform_score", "translations"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        base = [c for c in cols if c not in ("from_lat", "from_lon", "to_lat", "to_lon")]
        out[t] = sorted(con.execute(f"SELECT {', '.join(base)} FROM {t}").fetchall())
    return out


def test_bd_nueva_queda_en_la_ultima_version(env):
    from app import db
    from app.migrations import LATEST
    aplicadas = db.init()
    assert aplicadas == list(range(1, LATEST + 1))
    assert db.schema_version() == LATEST
    # Idempotente: una segunda vez no hace nada.
    assert db.init() == []


def test_migracion_conserva_datos_de_la_030(env):
    """R89: rutas, historial y andenes aprendidos sobreviven."""
    from app import db
    from app.config import settings
    antes = _poblar(settings.db_path)
    aplicadas = db.init()
    assert aplicadas == [1, 2]
    con = sqlite3.connect(settings.db_path)
    despues = _snapshot(con)
    assert despues == antes
    tablas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"devices", "pairing_codes", "quota_usage", "error_log", "settings_kv",
            "map_cache", "platform_score_v2"} <= tablas
    con.close()
    # La ruta se sigue leyendo igual por la API de datos.
    r = db.list_routes()
    assert len(r) == 1 and r[0]["name"] == "Casa → Trabajo"
    assert r[0]["legs"][0]["directions"] == ["Ermont - Eaubonne"]
    assert "from_lat" not in r[0]["legs"][0]       # el contrato 0.3.0 no cambia
    # Copia de seguridad hecha antes de migrar.
    assert os.path.exists(settings.db_path + ".bak-v0")


def test_migracion_recoge_bd_de_la_010(env):
    from app import db
    from app.config import settings
    con = sqlite3.connect(settings.db_path)
    con.executescript(SCHEMA_010_ROUTES)
    con.execute("INSERT INTO routes (name, origin_id, origin_name, dest_id, dest_name) "
                "VALUES ('vieja','a','A','b','B')")
    con.commit()
    con.close()
    db.init()
    r = db.list_routes()
    assert r[0]["time_mode"] == "window" and r[0]["duration_min"] == 0


def test_migracion_fallida_no_deja_esquema_a_medias(env, monkeypatch):
    from app import migrations
    from app.config import settings

    def rota(con):
        con.execute("CREATE TABLE a_medias (x)")
        raise RuntimeError("fallo a proposito")
    monkeypatch.setattr(migrations, "MIGRATIONS",
                        migrations.MIGRATIONS[:1] + [(2, "rota", rota)])
    con = sqlite3.connect(settings.db_path, isolation_level=None)
    with pytest.raises(RuntimeError):
        migrations.migrate(con, settings.db_path)
    assert migrations.current_version(con) == 1
    tablas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "a_medias" not in tablas
    con.close()


REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    ".local", "trajet-prod.db")


@pytest.mark.skipif(not os.path.exists(REAL), reason="no hay copia real de trajet.db en .local/")
def test_migracion_real_conserva_todo(env, tmp_path):
    """Contra la copia REAL de produccion (sobre una copia de la copia)."""
    from app import db
    from app.config import settings
    shutil.copy(REAL, settings.db_path)
    con = sqlite3.connect(settings.db_path)
    antes = _snapshot(con)
    con.close()
    db.init()
    con = sqlite3.connect(settings.db_path)
    assert _snapshot(con) == antes
    con.close()
