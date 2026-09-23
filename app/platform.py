"""Prevision del anden a partir de lo observado en dias anteriores.

Por que no un modelo de lenguaje: esto es contar. "El tren de las 08:12 a
Mantes salio de la via 21 diecisiete de las ultimas veinte veces" es una tabla
de frecuencias. Da el porcentaje exacto, cabe en el SQLite que ya hay,
responde en microsegundos y no puede inventarse una via que no existe.

Que se aprende, en orden de fiabilidad:

  1. Por numero de mision (el "135711" que trae TrainNumbers). Es el mismo
     tren todos los dias laborables y suele repetir anden. Es la senal buena.
  2. Por (linea, parada, destino, hora teorica, laborable/fin de semana).
     Sirve cuando no hay numero de mision.
  3. Por (linea, parada, destino, laborable/fin de semana), sin la hora.
     Es el ultimo recurso: dice "esta linea suele salir por aqui".

Solo se estudian las estaciones de las rutas guardadas. El sondeo del 30/08
midio que la via solo aparece en el 17 % de los trenes y con 7,7 min de
mediana; la prevision existe justo para cubrir el rato de antes.
"""
from __future__ import annotations

from datetime import datetime

from . import db
from .config import settings
from .idfm import norm_text

# Nivel minimo para ensenar algo. Por debajo la prevision engana mas que ayuda.
# El umbral es ESTRICTO: con 4 y 4 no hay mayoria, hay una moneda al aire, y
# eso no se pinta en pantalla.
MIN_SAMPLES = 3
MIN_SHARE = 0.5

# Peso de cada nivel al ordenar candidatos: la mision manda sobre la hora, y
# la hora sobre el "esta linea suele salir por aqui".
BASIS_RANK = {"mision": 0, "hora": 1, "linea": 2}
BASIS_LABEL = {
    "mision": "por el número de tren",
    "hora": "por la hora habitual",
    "linea": "por la línea",
}


def _daytype(weekday: int) -> str:
    """Fin de semana y laborable tienen horarios distintos: no se mezclan."""
    return "finde" if weekday >= 5 else "laborable"


def _hhmm(value: str | None) -> str:
    if not value:
        return ""
    v = str(value)
    # Acepta "2026-08-30T08:12:00Z", "08:12:00" y "08:12"
    if "T" in v:
        v = v.split("T", 1)[1]
    return v[:5]


# ---------------- recoger ----------------

def record(stop_id: str, line_id: str, dest: str, train: str | None,
           aimed: str | None, platform: str, when: datetime | None = None) -> bool:
    """Apunta un anden observado. Devuelve True si era nuevo para hoy."""
    if not (stop_id and line_id and platform):
        return False
    now = when or datetime.now(settings.tz)
    with db.conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO platform_obs "
            "(day, seen_at, stop_id, line_id, dest, train, aimed, weekday, platform) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (now.date().isoformat(), now.isoformat(timespec="seconds"),
             stop_id, line_id, norm_text(dest), (train or "").strip(),
             _hhmm(aimed), now.weekday(), str(platform).strip()))
        return cur.rowcount > 0


def record_board(board: dict, route: dict) -> int:
    """Recoge lo que se ve en un tablero ya construido. No gasta cuota extra."""
    by_seq = {leg["seq"]: leg for leg in route["legs"]}
    n = 0
    for leg in board.get("legs", []):
        src = by_seq.get(leg["seq"])
        if not src:
            continue
        for d in leg.get("departures", []):
            if not d.get("platform"):
                continue
            nuevo = record(src["from_id"], leg["line_id"],
                           d.get("destination", ""), d.get("train"),
                           d.get("aimed_at") or d.get("at"), d["platform"])
            if not nuevo:
                continue
            n += 1
            # Ya sabemos la via de verdad: se comprueba que habria dicho la
            # prevision SIN los datos de hoy, y se apunta si acerto.
            hoy = datetime.now(settings.tz).date().isoformat()
            antes = predict(src["from_id"], leg["line_id"],
                            d.get("destination", ""), d.get("train"),
                            d.get("aimed_at") or d.get("at"),
                            exclude_day=hoy)
            if antes:
                score(src["from_id"], leg["line_id"],
                      antes["platform"], d["platform"])
    return n


# ---------------- predecir ----------------

def _query(sql: str, args: tuple, exclude_day: str | None = None
           ) -> list[tuple[str, int]]:
    """exclude_day sirve para puntuar sin hacer trampa: al comprobar si la
    prevision acerto hay que preguntarle SIN los datos de hoy, que son
    justamente la respuesta."""
    if exclude_day:
        sql = sql.replace(" GROUP BY", " AND day<>? GROUP BY")
        args = args + (exclude_day,)
    with db.conn() as c:
        return [(r["platform"], r["n"]) for r in c.execute(sql, args)]


def _best(rows: list[tuple[str, int]], basis: str) -> tuple[dict | None, bool]:
    """Devuelve (prevision, concluyente).

    'concluyente' quiere decir que en este nivel YA hay datos suficientes, asi
    que no hay que seguir bajando a uno mas vago. Es la diferencia entre "no
    se todavia" y "lo se, y es que no hay una via clara": si el numero de
    mision dice que ese tren sale unas veces por la 21 y otras por la 17, un
    promedio de toda la linea que parezca seguro seria mentira.
    """
    total = sum(n for _, n in rows)
    if total < MIN_SAMPLES:
        return None, False
    platform, n = max(rows, key=lambda r: r[1])
    share = n / total
    if share <= MIN_SHARE:
        return None, True
    return {"platform": platform, "share": round(share, 2),
            "samples": total, "basis": basis,
            "why": BASIS_LABEL[basis]}, True


def predict(stop_id: str, line_id: str, dest: str, train: str | None,
            aimed: str | None, weekday: int | None = None,
            exclude_day: str | None = None) -> dict | None:
    """Anden mas probable, o None si todavia no hay con que decirlo."""
    if not (stop_id and line_id):
        return None
    wd = weekday if weekday is not None else datetime.now(settings.tz).weekday()
    finde = 1 if wd >= 5 else 0
    dest_n = norm_text(dest)
    hora = _hhmm(aimed)

    # 1) el mismo tren, por su numero de mision
    if train:
        rows = _query(
            "SELECT platform, COUNT(*) n FROM platform_obs "
            "WHERE stop_id=? AND line_id=? AND train=? AND train<>'' "
            "GROUP BY platform", (stop_id, line_id, str(train).strip()),
            exclude_day)
        hit, zanjado = _best(rows, "mision")
        if hit or zanjado:
            return hit

    # 2) misma linea, destino y hora teorica, en el mismo tipo de dia
    if hora:
        rows = _query(
            "SELECT platform, COUNT(*) n FROM platform_obs "
            "WHERE stop_id=? AND line_id=? AND dest=? AND aimed=? "
            "  AND (weekday>=5)=? GROUP BY platform",
            (stop_id, line_id, dest_n, hora, finde), exclude_day)
        hit, zanjado = _best(rows, "hora")
        if hit or zanjado:
            return hit

    # 3) ultimo recurso: por donde suele salir esta linea hacia ese destino
    rows = _query(
        "SELECT platform, COUNT(*) n FROM platform_obs "
        "WHERE stop_id=? AND line_id=? AND dest=? AND (weekday>=5)=? "
        "GROUP BY platform", (stop_id, line_id, dest_n, finde), exclude_day)
    return _best(rows, "linea")[0]


def annotate(board: dict, route: dict) -> None:
    """Rellena 'guess' en cada salida que aun no tenga anden de verdad."""
    by_seq = {leg["seq"]: leg for leg in route["legs"]}
    for leg in board.get("legs", []):
        src = by_seq.get(leg["seq"])
        if not src:
            continue
        for d in leg.get("departures", []):
            if d.get("platform"):
                continue
            g = predict(src["from_id"], leg["line_id"], d.get("destination", ""),
                        d.get("train"), d.get("aimed_at") or d.get("at"))
            if g:
                d["guess"] = g


# ---------------- saber si sirve ----------------

def score(stop_id: str, line_id: str, predicted: str, actual: str,
          when: datetime | None = None) -> None:
    """Apunta si la prevision acerto, cuando aparece el anden de verdad."""
    now = when or datetime.now(settings.tz)
    with db.conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO platform_score "
            "(day, stop_id, line_id, predicted, actual, hit) VALUES (?,?,?,?,?,?)",
            (now.date().isoformat(), stop_id, line_id, predicted, actual,
             1 if predicted == actual else 0))


def accuracy() -> dict:
    """Cuantas veces acerto la prevision. Sin maquillaje."""
    with db.conn() as c:
        r = c.execute(
            "SELECT COUNT(*) n, SUM(hit) ok FROM platform_score").fetchone()
        obs = c.execute("SELECT COUNT(*) n FROM platform_obs").fetchone()["n"]
        dias = c.execute(
            "SELECT COUNT(DISTINCT day) n FROM platform_obs").fetchone()["n"]
    n = r["n"] or 0
    ok = r["ok"] or 0
    return {
        "predictions": n,
        "hits": ok,
        "rate": round(ok / n, 2) if n else None,
        "observations": obs,
        "days": dias,
    }


def coverage(route: dict) -> list[dict]:
    """Cuanto sabe ya de cada tramo de una ruta, para poder decirlo en pantalla."""
    out = []
    for leg in route["legs"]:
        with db.conn() as c:
            r = c.execute(
                "SELECT COUNT(*) n, COUNT(DISTINCT day) d, "
                "       COUNT(DISTINCT platform) v "
                "FROM platform_obs WHERE stop_id=? AND line_id=?",
                (leg["from_id"], leg["line_id"])).fetchone()
        out.append({
            "seq": leg["seq"], "line_code": leg["line_code"],
            "observations": r["n"], "days": r["d"], "platforms": r["v"],
        })
    return out
