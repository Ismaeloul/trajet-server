"""Comprueba el horario de una ruta: franja, hora de salida y hora de llegada.

No toca la API. Tambien comprueba que una base de datos de la version anterior
se migra sin perder nada, que es lo que puede romperle la app a alguien que ya
la tenia instalada.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PATH = os.path.join(tempfile.mkdtemp(), "vieja.db")
os.environ["TRAJET_DB"] = PATH

FAILS = []
LEG = [{"line_id": "line:IDFM:C01739", "from_id": "stop_area:IDFM:71370"}]


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'MAL'} {name}" + ("" if cond else f"   <<< {extra}"))
    if not cond:
        FAILS.append(name)


def esquema_viejo():
    """El de la primera version: sin time_mode, time_at ni duration_min."""
    con = sqlite3.connect(PATH)
    con.executescript("""
    CREATE TABLE routes (
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      origin_id TEXT NOT NULL, origin_name TEXT NOT NULL,
      dest_id TEXT NOT NULL, dest_name TEXT NOT NULL,
      days TEXT NOT NULL DEFAULT '0,1,2,3,4',
      time_from TEXT NOT NULL DEFAULT '07:00',
      time_to TEXT NOT NULL DEFAULT '10:00',
      position INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT (datetime('now')));
    INSERT INTO routes (name, origin_id, origin_name, dest_id, dest_name,
                        time_from, time_to)
      VALUES ('generica de antes','a','A','b','B','07:00','22:00');
    """)
    con.commit()
    con.close()


def main():
    esquema_viejo()
    from app import db
    from app.board import derive_window, pick_active_route
    from app.config import settings

    print("=== migracion desde la version anterior ===")
    db.init()
    vieja = db.list_routes()[0]
    check("la ruta antigua sobrevive", vieja["name"] == "generica de antes", vieja)
    check("y hereda el modo de franja", vieja["time_mode"] == "window",
          vieja["time_mode"])
    db.init()
    check("volver a migrar no rompe nada", len(db.list_routes()) == 1)

    print("\n=== las tres formas de decir el horario ===")
    casos = [
        ({"time_mode": "window", "time_from": "07:00", "time_to": "10:00"},
         ("07:00", "10:00"), "franja de toda la vida"),
        ({"time_mode": "departure", "time_at": "08:00", "duration_min": 45},
         ("07:15", "09:15"), "salgo a las 08:00"),
        ({"time_mode": "arrival", "time_at": "09:00", "duration_min": 45},
         ("07:30", "09:15"), "llego a las 09:00"),
        ({"time_mode": "arrival", "time_at": "09:00", "duration_min": 0},
         ("07:15", "09:15"), "llego a las 09:00, sin saber cuanto dura"),
        ({"time_mode": "arrival", "time_at": "00:30", "duration_min": 45},
         ("00:00", "00:45"), "no se sale del dia por abajo"),
        ({"time_mode": "departure", "time_at": "23:30", "duration_min": 60},
         ("22:45", "23:59"), "ni por arriba"),
        ({"time_mode": "arrival", "time_at": ""},
         ("07:00", "10:00"), "sin hora, se queda la franja"),
    ]
    for r, esperado, que in casos:
        r.setdefault("time_from", "07:00")
        r.setdefault("time_to", "10:00")
        got = derive_window(r)
        check(que, got == esperado, f"{got} en vez de {esperado}")

    print("\n=== se guarda y se relee ===")
    rid = db.save_route({
        "name": "llego a las 9", "origin_id": "a", "origin_name": "A",
        "dest_id": "b", "dest_name": "B", "days": [0, 1, 2, 3, 4],
        "time_mode": "arrival", "time_at": "09:00", "duration_min": 38,
        "legs": LEG})
    n = db.get_route(rid)
    check("guarda el modo", n["time_mode"] == "arrival", n["time_mode"])
    check("guarda la hora", n["time_at"] == "09:00", n["time_at"])
    check("guarda la duracion real del itinerario", n["duration_min"] == 38)
    check("y deja calculada la franja",
          (n["time_from"], n["time_to"]) == ("07:37", "09:15"),
          (n["time_from"], n["time_to"]))

    db.save_route({
        "name": "salgo a las 18", "origin_id": "b", "origin_name": "B",
        "dest_id": "a", "dest_name": "A", "days": [0, 1, 2, 3, 4],
        "time_mode": "departure", "time_at": "18:00", "duration_min": 40,
        "legs": LEG})

    print("\n=== que ruta se ensena sola ===")
    # La generica abarca de 07:00 a 22:00 y se solapa con las otras dos. Debe
    # ganar siempre la mas concreta, no la que este antes en la lista.
    todas = db.list_routes()
    for (h, m), esperada in (((8, 30), "llego a las 9"),
                             ((18, 10), "salgo a las 18"),
                             ((12, 0), "generica de antes")):
        act = pick_active_route(todas, datetime(2026, 8, 31, h, m,
                                                tzinfo=settings.tz))
        check(f"a las {h:02d}:{m:02d} toca '{esperada}'",
              act["name"] == esperada, act["name"])

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLOS: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
