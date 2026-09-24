"""Prevision de via y su acierto (app/platform.py): R11, R75, R76.

Fallos de docs/servidor.md §18.3 que se arreglan aqui, cada uno con su test:
  9  el acierto se cuenta POR TREN (platform_score_v2), no por combinacion
  10 puntua el primero que ve la via, sea el tablero o el recolector
  12 sin hora teorica no se usa la prevista en la clave unica de platform_obs

Incluye todo lo que comprobaba tools/test_platform.py sobre la prevision
(lo del recolector esta en tests/test_collector.py). Trabaja sobre una BD
temporal con observaciones inventadas, para poder afirmar cosas exactas.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from _seg_contrato import schema_validator
from conftest import ruta_j
from fakeprim import Dep

from app import platform
from app.config import settings

STOP = "stop_area:IDFM:71370"
LINE = "line:IDFM:C01739"


@pytest.fixture
def bd(env):
    from app import db
    db.init()
    platform._forget_accuracy()
    return env


def dia(n: int) -> datetime:
    """n dias antes de hoy, a las 08:12 hora de Paris."""
    base = datetime.now(settings.tz).replace(hour=8, minute=12, second=0, microsecond=0)
    return base - timedelta(days=n)


def misma_semana(n: int) -> datetime:
    """n semanas antes: mismo dia de la semana que hoy (los niveles 2 y 3
    separan laborable de fin de semana a proposito)."""
    return dia(7 * n)


def _obs(dest, train, aimed, via, when) -> bool:
    return platform.record(STOP, LINE, dest, train, aimed, via, when=when)


def _filas(tabla: str) -> list[dict]:
    from app import db
    with db.conn() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {tabla} ORDER BY id")]


# =====================================================================
#  Prevision (lo de tools/test_platform.py)
# =====================================================================

def test_sin_datos_no_se_inventa_nada(bd):
    assert platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12") is None
    _obs("Mantes-la-Jolie", "135711", "08:12", "21", dia(1))
    g = platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12")
    assert g is None, f"con 1 observacion sigue callado (minimo {platform.MIN_SAMPLES})"


def test_el_mismo_tren_varios_dias(bd):
    for n in (1, 2, 3, 4):
        _obs("Mantes-la-Jolie", "135711", "08:12", "21", dia(n))
    g = platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12")
    assert g == {"platform": "21", "share": 1.0, "samples": 4, "basis": "mision",
                 "why": "por el número de tren"}

    # El mismo dia repetido no cuenta mas veces: refrescar no infla nada.
    antes = platform.accuracy()["observations"]
    for _ in range(5):
        assert _obs("Mantes-la-Jolie", "135711", "08:12", "21", dia(2)) is False
    assert platform.accuracy()["observations"] == antes == 4

    # Si el anden cambia, baja la confianza pero sigue la mayoritaria.
    for n in (5, 6):
        _obs("Mantes-la-Jolie", "135711", "08:12", "17", dia(n))
    g = platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12")
    assert g["platform"] == "21" and g["share"] == 0.67 and g["samples"] == 6

    # Con 4 y 4 no hay mayoria: se calla (y no baja a un nivel mas vago).
    for n in (7, 8):
        _obs("Mantes-la-Jolie", "135711", "08:12", "17", dia(n))
    assert platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12") is None


def test_sin_numero_de_tren_cae_a_la_hora_y_normaliza_el_destino(bd):
    for n in range(1, 5):
        _obs("Rouen Rive Droite", None, "09:40", "23", misma_semana(n))
    g = platform.predict(STOP, LINE, "Rouen Rive Droite", None, "09:40")
    assert g["platform"] == "23" and g["basis"] == "hora" and g["why"] == "por la hora habitual"
    # Acentos, mayusculas y espacios finos/duros no rompen el emparejado.
    assert platform.predict(STOP, LINE, "Rouen Rive Droite", None, "09:40")["platform"] == "23"
    assert platform.predict(STOP, LINE, "ROUEN RIVE DROITE", None, "09:40")["platform"] == "23"
    # Acepta la hora con fecha ISO o con segundos.
    assert platform.predict(STOP, LINE, "Rouen Rive Droite", None,
                            "2026-08-30T09:40:00Z")["platform"] == "23"
    # Laborable y fin de semana no se mezclan.
    hoy = datetime.now(settings.tz)
    otro = 0 if hoy.weekday() >= 5 else 5
    assert platform.predict(STOP, LINE, "Rouen Rive Droite", None, "09:40", weekday=otro) is None


def test_prevision_umbral_niveles_tipo_dia(bd):
    """R75: >= 3 observaciones y mayoria ESTRICTA (> 50 %); mision > hora >
    linea; laborable y fin de semana aparte en hora y linea."""
    # Umbral: 2 no bastan; 3 si.
    for n in (1, 2):
        _obs("Le Havre", "111", "10:00", "5", misma_semana(n))
    assert platform.predict(STOP, LINE, "Le Havre", "111", "10:00") is None
    _obs("Le Havre", "111", "10:00", "5", misma_semana(3))
    assert platform.predict(STOP, LINE, "Le Havre", "111", "10:00")["samples"] == 3
    # Mayoria estricta: 2 de 4 (50 %) no; 3 de 5 (60 %) si.
    for n, via in ((1, "7"), (2, "7")):
        _obs("Caen", "222", "", via, dia(n))
    for n, via in ((3, "8"), (4, "8")):
        _obs("Caen", "222", "", via, dia(n))
    assert platform.predict(STOP, LINE, "Caen", "222", None) is None
    _obs("Caen", "222", "", "7", dia(5))
    assert platform.predict(STOP, LINE, "Caen", "222", None)["share"] == 0.6

    # Niveles: un tren nuevo (sin historico por mision) usa la hora; sin hora
    # teorica conocida, la linea.
    for n in range(1, 4):
        _obs("Vernon", f"9{n}", "07:05", "3", misma_semana(n))     # misiones distintas
    for n in range(1, 3):
        _obs("Vernon", f"8{n}", "18:30", "4", misma_semana(n))
    g = platform.predict(STOP, LINE, "Vernon", "99999", "07:05")
    assert g["basis"] == "hora" and g["platform"] == "3"
    g = platform.predict(STOP, LINE, "Vernon", "99999", "12:00")
    assert g["basis"] == "linea" and g["platform"] == "3" and g["samples"] == 5
    assert g["share"] == 0.6 and g["why"] == "por la línea"
    # La mision manda sobre la hora cuando tiene datos.
    for n in range(1, 4):
        _obs("Vernon", "4444", "07:05", "9", dia(n))
    assert platform.predict(STOP, LINE, "Vernon", "4444", "07:05")["platform"] == "9"
    # Fin de semana: el historico de laborables no vale.
    hoy = datetime.now(settings.tz)
    otro = 0 if hoy.weekday() >= 5 else 5
    assert platform.predict(STOP, LINE, "Vernon", "99999", "07:05", weekday=otro) is None


def test_prevision_linea_sin_mayoria_no_se_pinta(bd):
    for n in range(1, 4):
        _obs("Vernon", "", "07:05", "3", misma_semana(n))
        _obs("Vernon", "", "18:30", "4", misma_semana(n))
    assert platform.predict(STOP, LINE, "Vernon", None, "12:00") is None


def test_annotate_solo_sin_via_real(bd):
    for n in range(1, 4):
        _obs("Ermont - Eaubonne", "135711", "08:12", "21", dia(n))
    route = {"legs": [{"seq": 0, "from_id": STOP, "line_id": LINE}]}
    board = {"legs": [{"seq": 0, "line_id": LINE, "departures": [
        {"destination": "Ermont - Eaubonne", "train": "135711", "aimed_at": "08:12",
         "at": "08:14", "platform": None},
        {"destination": "Ermont - Eaubonne", "train": "135711", "aimed_at": "08:12",
         "at": "08:14", "platform": "17"}]}]}
    platform.annotate(board, route)
    sin, con = board["legs"][0]["departures"]
    assert sin["guess"]["platform"] == "21" and "guess" not in con
    schema_validator("PlatformGuess").validate(sin["guess"])


# =====================================================================
#  Fallo 18.3.12: sin hora teorica, la prevista no entra en la clave
# =====================================================================

def test_sin_hora_teorica_una_fila_por_tren(bd, make_route):
    """El mismo tren sin hora teorica, visto con la prevista moviendose por
    el retraso, es UNA observacion (antes, una por cada minuto de retraso)."""
    from app import db
    route = db.get_route(make_route())
    for prevista in ("08:12", "08:13", "08:15", "08:19"):
        board = {"legs": [{"seq": 0, "line_id": LINE, "departures": [
            {"destination": "Ermont - Eaubonne", "train": "135711", "aimed_at": "",
             "at": prevista, "platform": "21", "jid": "SNCF:VJ:1"}]}]}
        platform.record_board(board, route)
    filas = _filas("platform_obs")
    assert len(filas) == 1 and filas[0]["aimed"] == "" and filas[0]["train"] == "135711"
    # Con hora teorica se guarda la teorica, nunca la prevista.
    board = {"legs": [{"seq": 0, "line_id": LINE, "departures": [
        {"destination": "Ermont - Eaubonne", "train": "135715", "aimed_at": "08:40",
         "at": "08:47", "platform": "19", "jid": "SNCF:VJ:2"}]}]}
    platform.record_board(board, route)
    assert _filas("platform_obs")[-1]["aimed"] == "08:40"


# =====================================================================
#  Acierto: R11, R76 y fallos 18.3.9 y 18.3.10
# =====================================================================

def _historico(train: str, via: str, dias=(1, 2, 3), dest="Ermont - Eaubonne",
               aimed="08:12"):
    for n in dias:
        _obs(dest, train, aimed, via, dia(n))


def _dep(train, via, aimed="08:12", at="08:14", dest="Ermont - Eaubonne", jid=""):
    return {"destination": dest, "train": train, "aimed_at": aimed, "at": at,
            "platform": via, "jid": jid or f"SNCF:VJ:{train}"}


def test_puntuacion_sin_trampa(bd):
    """R76: al aparecer la via de verdad se pregunta a la prevision que habria
    dicho SIN los datos de hoy, que son justamente la respuesta."""
    hoy = datetime.now(settings.tz).date().isoformat()
    for n in (1, 2, 3):
        _obs("Le Havre", "999", "10:00", "5", misma_semana(n))
    _obs("Le Havre", "999", "10:00", "9", dia(0))     # hoy ya se vio la 9
    # Con hoy, 4 observaciones; sin hoy, 3 y la prevision limpia dice la 5.
    con_hoy = platform.predict(STOP, LINE, "Le Havre", "999", "10:00")
    sin_hoy = platform.predict(STOP, LINE, "Le Havre", "999", "10:00", exclude_day=hoy)
    assert con_hoy["samples"] == 4 and sin_hoy["samples"] == 3
    assert sin_hoy["platform"] == "5"

    # Hoy sale por la 9: se puntua como FALLO, aunque hoy ya hubiera datos.
    assert platform.score_departure(STOP, LINE, _dep("999", "9", "10:00", dest="Le Havre")) is True
    acc = platform.accuracy()
    assert acc["predictions"] == 1 and acc["hits"] == 0 and acc["rate"] == 0.0
    fila = _filas("platform_score_v2")[0]
    assert (fila["predicted"], fila["actual"], fila["hit"], fila["basis"]) == ("5", "9", 0, "mision")
    assert fila["day"] == hoy and fila["train_key"] == "m:999"


def test_acierto_por_tren(bd):
    """Fallo 18.3.9: diez trenes que aciertan «21 -> 21» el mismo dia y linea
    son diez aciertos, no una fila."""
    for i in range(10):
        _historico(f"10{i}", "21")
    _historico("200", "21")
    for i in range(10):
        assert platform.score_departure(STOP, LINE, _dep(f"10{i}", "21")) is True
    assert platform.score_departure(STOP, LINE, _dep("200", "17")) is True
    # El mismo tren otra vez (40 refrescos de pantalla) no cuenta mas.
    for _ in range(3):
        assert platform.score_departure(STOP, LINE, _dep("100", "21")) is False
    acc = platform.accuracy()
    assert acc["predictions"] == 11 and acc["hits"] == 10 and acc["rate"] == 0.91
    # Un tren sin prevision posible no se puntua (no hubo prevision).
    assert platform.score_departure(STOP, LINE, _dep("777", "3", aimed="23:50",
                                                     dest="Gisors")) is False
    assert platform.accuracy()["predictions"] == 11


def test_train_key():
    """Numero de mision; si no, linea + destino + hora teorica; si no, el
    viaje de SIRI. Nunca la hora prevista."""
    assert platform.train_key(LINE, "Ermont", "135711", "08:12") == "m:135711"
    assert platform.train_key(LINE, "Ermont – Eaubonne", None, "08:12") == \
        "h:C01739|ermont - eaubonne|08:12"
    assert platform.train_key(LINE, "Ermont", "", "", "SNCF:VJ:9") == "j:SNCF:VJ:9"
    assert platform.train_key(LINE, "Ermont", None, None) == ""


def test_puntua_sin_numero_ni_hora_por_el_viaje(bd):
    for n in (1, 2, 3):
        _obs("Pontoise", "", "", "3", misma_semana(n))
    dep = {"destination": "Pontoise", "train": None, "aimed_at": "", "at": "08:14",
           "platform": "3", "jid": "SNCF:VJ:77"}
    assert platform.score_departure(STOP, LINE, dep) is True
    assert _filas("platform_score_v2")[0]["train_key"] == "j:SNCF:VJ:77"
    assert platform.score_departure(STOP, LINE, dict(dep, at="08:20")) is False
    # Sin nada que lo identifique no se puntua.
    assert platform.score_departure(STOP, LINE, dict(dep, jid="")) is False


def test_tablero_puntua_aunque_la_observacion_ya_exista(bd, make_route):
    """Fallo 18.3.10: si la via ya estaba apuntada hoy (la vio otro), el
    tablero puntua igual en vez de callarse."""
    from app import db
    route = db.get_route(make_route())
    _historico("135711", "21")
    hoy = datetime.now(settings.tz)
    assert _obs("Ermont - Eaubonne", "135711", "08:12", "21", hoy) is True
    board = {"legs": [{"seq": 0, "line_id": LINE, "departures": [_dep("135711", "21")]}]}
    assert platform.record_board(board, route) == 0           # la observacion no es nueva
    assert platform.accuracy()["predictions"] == 1 and platform.accuracy()["hits"] == 1
    platform.record_board(board, route)
    assert len(_filas("platform_score_v2")) == 1


async def test_recolector_primero_tambien_puntua(env, fake_prim, make_route):
    """Fallo 18.3.10: el recolector ve la via antes que la pantalla y puntua
    el; la pantalla, despues, no puntua dos veces el mismo tren."""
    from app import collector, prim
    from app.api import common
    make_route()
    _historico("135711", "21")
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6, platform="21",
                               train="135711", jid="SNCF:VJ:J:135711"))
    await prim.startup()
    try:
        tarde = datetime.now(settings.tz).replace(hour=14, minute=0)
        res = await collector.sample_once(now=tarde)
        assert res["recorded"] == 1
        filas = _filas("platform_score_v2")
        assert len(filas) == 1 and filas[0]["hit"] == 1 and filas[0]["train_key"] == "m:135711"

        data = await common.board(None, False)
        assert data["legs"][0]["departures"][0]["platform"] == "21"
        assert len(_filas("platform_score_v2")) == 1
        acc = await common.platform_model(None)
        assert acc["accuracy"]["predictions"] == 1 and acc["accuracy"]["rate"] == 1.0
    finally:
        await prim.shutdown()


def test_platform_model_accuracy(client, fake_prim, make_route):
    """R11: el porcentaje sale solo de /api/platform-model (y /api/health),
    con la forma PlatformAccuracy del contrato, y cuenta trenes."""
    make_route(ruta_j())
    _historico("135711", "21")
    _historico("135713", "21")
    fake_prim.add("71370",
                  Dep("C01739", "Ermont - Eaubonne", 4, platform="21", train="135711"),
                  Dep("C01739", "Ermont - Eaubonne", 9, platform="19", train="135713"))
    assert client.get("/api/board").status_code == 200
    body = client.get("/api/platform-model").json()
    schema_validator("PlatformAccuracy").validate(body["accuracy"])
    assert body["accuracy"]["predictions"] == 2 and body["accuracy"]["hits"] == 1
    assert body["accuracy"]["rate"] == 0.5
    health = client.get("/api/health").json()["platform_model"]
    assert health == body["accuracy"]


def test_accuracy_cae_a_la_tabla_vieja(bd):
    """Recien actualizado, platform_score_v2 esta vacia: se ensena la de la
    0.3.0 (que se conserva intacta) hasta que haya algun tren puntuado."""
    from app import db
    vacio = platform.accuracy()
    assert vacio == {"predictions": 0, "hits": 0, "rate": None, "observations": 0, "days": 0}
    with db.conn() as c:
        c.executemany("INSERT INTO platform_score (day, stop_id, line_id, predicted, actual, hit) "
                      "VALUES (?,?,?,?,?,?)",
                      [("2026-08-30", STOP, LINE, "21", "21", 1),
                       ("2026-08-30", STOP, LINE, "21", "17", 0),
                       ("2026-08-31", STOP, LINE, "21", "21", 1)])
    platform._forget_accuracy()
    vieja = platform.accuracy()
    assert (vieja["predictions"], vieja["hits"], vieja["rate"]) == (3, 2, 0.67)

    platform.score(STOP, LINE, "m:1", "5", "5", basis="mision")
    nueva = platform.accuracy()
    assert (nueva["predictions"], nueva["hits"], nueva["rate"]) == (1, 1, 1.0)
    assert len(_filas("platform_score")) == 3                 # el historico sigue ahi


def test_accuracy_en_cache_unos_segundos(bd, monkeypatch):
    """/api/health la llama cada 30 s: no cuenta la BD entera cada vez, y en
    cuanto se apunta algo nuevo se vuelve a contar."""
    from app import db
    real = db.conn
    aperturas = []

    def contada():
        aperturas.append(1)
        return real()

    monkeypatch.setattr(db, "conn", contada)
    a = platform.accuracy()
    n = len(aperturas)
    assert platform.accuracy() == a and len(aperturas) == n      # de la cache
    platform.score(STOP, LINE, "m:1", "5", "7")
    assert platform.accuracy()["predictions"] == 1               # se olvido al puntuar
    # Pasado el tiempo, se vuelve a contar aunque nadie haya escrito.
    n = len(aperturas)
    monkeypatch.setattr(platform, "ACCURACY_TTL", 0.0)
    platform.accuracy()
    assert len(aperturas) > n


def test_coverage_por_tramo(bd):
    for n in (1, 2):
        _obs("Ermont - Eaubonne", "1", "08:12", "21", dia(n))
    _obs("Ermont - Eaubonne", "2", "08:40", "19", dia(1))
    route = {"legs": [{"seq": 0, "line_code": "J", "from_id": STOP, "line_id": LINE},
                      {"seq": 1, "line_code": "13", "from_id": STOP,
                       "line_id": "line:IDFM:C01383"}]}
    assert platform.coverage(route) == [
        {"seq": 0, "line_code": "J", "observations": 3, "days": 2, "platforms": 2},
        {"seq": 1, "line_code": "13", "observations": 0, "days": 0, "platforms": 0}]
