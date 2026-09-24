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

Si acierta se sabe cuando aparece la via de verdad: se le pregunta que habria
dicho sin los datos de hoy y se apunta, una vez por tren y dia, en
`platform_score_v2` (learn / score_departure). Lo hace el primero que vea la
via, el tablero o el recolector.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from . import db
from .config import settings
from .idfm import line_code, norm_text

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


def train_key(line_id: str, dest: str, train: str | None, aimed: str | None,
              jid: str = "") -> str:
    """Identidad de un tren en un dia, para puntuar la prevision UNA vez por tren.

    El numero de mision si lo hay; si no, linea + destino + hora TEORICA; si
    tampoco hay hora teorica, el identificador del viaje de SIRI (estable
    durante el dia). Nunca la hora prevista: se mueve con el retraso y el
    mismo tren pareceria otro en cada refresco. "" = no se sabe que tren es y
    no se puntua.
    """
    train = str(train or "").strip()
    if train:
        return f"m:{train}"
    hora = _hhmm(aimed)
    if hora:
        return f"h:{line_code(line_id)}|{norm_text(dest)}|{hora}"
    jid = str(jid or "").strip()
    return f"j:{jid}" if jid else ""


# ---------------- recoger ----------------

def record(stop_id: str, line_id: str, dest: str, train: str | None,
           aimed: str | None, platform: str, when: datetime | None = None) -> bool:
    """Apunta un anden observado. Devuelve True si era nuevo para hoy.

    `aimed` es la hora TEORICA o nada. La prevista no vale: entra en el indice
    unico (dia, parada, linea, tren, hora, destino) y, como cambia con el
    retraso, el mismo tren sumaba una fila por cada minuto que se movia
    (fallo 18.3.12). Sin hora teorica se guarda "" y el tren lo distingue su
    numero de mision.
    """
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
        nuevo = cur.rowcount > 0
    if nuevo:
        _forget_accuracy()
    return nuevo


def learn(stop_id: str, line_id: str, dep: dict, when: datetime | None = None) -> bool:
    """Una salida con via real: se apunta la via y se puntua la prevision.

    Lo usan el tablero y el recolector por igual, asi que puntua el primero
    que vea la via, sea quien sea (en la 0.3.0 solo puntuaba el tablero y
    solo si la observacion era nueva: si el recolector la veia antes, ese
    tren no se puntuaba nunca, fallo 18.3.10). Devuelve True si la
    observacion era nueva para hoy.
    """
    if not dep.get("platform"):
        return False
    now = when or datetime.now(settings.tz)
    nuevo = record(stop_id, line_id, dep.get("destination", ""), dep.get("train"),
                   dep.get("aimed_at"), dep["platform"], when=now)
    score_departure(stop_id, line_id, dep, when=now)
    return nuevo


def learn_many(stop_id: str, seen: list[tuple[str, dict]],
               when: datetime | None = None) -> int:
    """learn() de varias salidas de una estacion: [(line_id, salida), ...]."""
    return sum(1 for line_id, dep in seen if learn(stop_id, line_id, dep, when=when))


def record_board(board: dict, route: dict) -> int:
    """Recoge lo que se ve en un tablero ya construido. No gasta cuota extra."""
    by_seq = {leg["seq"]: leg for leg in route["legs"]}
    now = datetime.now(settings.tz)
    n = 0
    for leg in board.get("legs", []):
        src = by_seq.get(leg["seq"])
        if not src:
            continue
        for d in leg.get("departures", []):
            if d.get("platform") and learn(src["from_id"], leg["line_id"], d, when=now):
                n += 1
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

def _scored(c: sqlite3.Connection, day: str, stop_id: str, line_id: str, key: str) -> bool:
    return c.execute(
        "SELECT 1 FROM platform_score_v2 WHERE day=? AND stop_id=? AND line_id=? "
        "AND train_key=?", (day, stop_id, line_id, key)).fetchone() is not None


def score_departure(stop_id: str, line_id: str, dep: dict,
                    when: datetime | None = None) -> bool:
    """Puntua la prevision de este tren si aun no se habia puntuado hoy.

    Se pregunta a la prevision que habria dicho SIN los datos de hoy (R76):
    los de hoy son justamente la respuesta. Si no se habria atrevido a decir
    nada, no hay nada que puntuar. Devuelve True si apunto una puntuacion.
    """
    actual = dep.get("platform")
    key = train_key(line_id, dep.get("destination", ""), dep.get("train"),
                    dep.get("aimed_at"), dep.get("jid", ""))
    if not (stop_id and line_id and actual and key):
        return False
    now = when or datetime.now(settings.tz)
    hoy = now.date().isoformat()
    with db.conn() as c:
        if _scored(c, hoy, stop_id, line_id, key):
            return False
    # La misma pregunta que hace annotate() para pintar la via probable (con
    # la prevista si no hay teorica), pero sin los datos de hoy.
    antes = predict(stop_id, line_id, dep.get("destination", ""), dep.get("train"),
                    dep.get("aimed_at") or dep.get("at"), weekday=now.weekday(),
                    exclude_day=hoy)
    if not antes:
        return False
    return score(stop_id, line_id, key, antes["platform"], actual,
                 basis=antes["basis"], when=now)


def score(stop_id: str, line_id: str, key: str, predicted: str, actual: str,
          basis: str = "", when: datetime | None = None) -> bool:
    """Apunta si la prevision acerto para UN tren (una fila por tren y dia).

    La tabla de la 0.3.0 (`platform_score`) tenia como clave la combinacion
    prevision/realidad: diez aciertos "21 -> 21" de la misma linea y dia eran
    una sola fila y la tasa no contaba trenes (fallo 18.3.9). Se conserva
    intacta como historico; lo nuevo va a `platform_score_v2`.
    """
    now = when or datetime.now(settings.tz)
    with db.conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO platform_score_v2 "
            "(day, stop_id, line_id, train_key, predicted, actual, hit, basis) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (now.date().isoformat(), stop_id, line_id, key,
             str(predicted), str(actual), 1 if str(predicted) == str(actual) else 0,
             basis or ""))
        nuevo = cur.rowcount > 0
    if nuevo:
        _forget_accuracy()
    return nuevo


# /api/health la llama el HEALTHCHECK cada 30 s y la app al abrir Ajustes:
# con decenas de miles de observaciones, contar en cada llamada es trabajo
# tirado. Se guarda unos segundos y se olvida en cuanto se apunta algo nuevo.
# La clave lleva la ruta de la BD: los tests (y un cambio de BD) no se pisan.
ACCURACY_TTL = 30.0
_accuracy_cache: tuple[str, float, dict] | None = None


def _forget_accuracy() -> None:
    global _accuracy_cache
    _accuracy_cache = None


def accuracy() -> dict:
    """Cuantas veces acerto la prevision. Sin maquillaje.

    `predictions` son trenes puntuados (uno por tren y dia) y `hits`, los que
    acerto. Hasta que la tabla nueva tenga algun tren se ensena la de la 0.3.0
    para que el porcentaje no desaparezca al actualizar. La forma es la de
    siempre (PlatformAccuracy en docs/openapi.yaml).
    """
    global _accuracy_cache
    hit = _accuracy_cache
    ahora = time.monotonic()
    if hit and hit[0] == settings.db_path and ahora - hit[1] < ACCURACY_TTL:
        return dict(hit[2])
    res = _count_accuracy()
    _accuracy_cache = (settings.db_path, ahora, res)
    return dict(res)


def _count_accuracy() -> dict:
    with db.conn() as c:
        r = None
        try:
            r = c.execute(
                "SELECT COUNT(*) n, SUM(hit) ok FROM platform_score_v2").fetchone()
        except sqlite3.OperationalError:     # BD sin migrar (modo degradado)
            r = None
        if not r or not r["n"]:
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
    with db.conn() as c:                  # una conexion para todos los tramos
        for leg in route["legs"]:
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
