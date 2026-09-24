"""Tablero (app/board.py): franjas, ruta que toca, avisos, salidas,
historial y resiliencia.

Reglas: R4, R5, R28, R37, R38, R40, R68, R69, R71, R72, R74, R82. Fallos de
docs/servidor.md §18.3 que se arreglan aqui: 11 (el recolector no toca la
memoria de via nueva), 13 (ruta sin franja y franjas que cruzan medianoche),
18 (nulls de SIRI) y 19 (el «hoy» de los avisos es el de Paris).

Incluye lo que comprobaban tools/test_routes.py (franjas, ruta activa y
migracion desde la primera version) y tools/test_resilience.py (cache, cuota,
copia con su edad, lock y tramo roto), ya sin scripts sueltos.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from conftest import ruta_j
from fakeprim import Dep, FakePrim, Msg
from jsonschema import Draft202012Validator

from app import board as B
from app.config import settings

HERE = os.path.dirname(os.path.abspath(__file__))
SL = "STIF:StopArea:SP:71370:"
J = {"line_id": "line:IDFM:C01739", "directions": ["Ermont - Eaubonne"]}


def _board_030() -> Draft202012Validator:
    with open(os.path.join(HERE, "contract", "openapi-0.3.0.yaml"), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    return Draft202012Validator({"$ref": "#/components/schemas/Board",
                                 "components": spec["components"]})


def _sm(*deps: Dep, now: datetime | None = None) -> dict:
    """Respuesta de stop-monitoring de Saint-Lazare con esos pasos."""
    fp = FakePrim(now=now)
    fp.add("71370", *deps)
    return fp._stop_monitoring(SL)


def _gm(*msgs: Msg, now: datetime | None = None) -> dict:
    fp = FakePrim(now=now)
    fp.message(*msgs)
    return fp._general_message()


def _paris(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=settings.tz)


# =====================================================================
#  R38: la franja de una ruta
# =====================================================================

def _w(**r) -> tuple[str, str]:
    r.setdefault("time_from", "07:00")
    r.setdefault("time_to", "10:00")
    return B.derive_window(r)


def test_derive_window_salida():
    """R38: «salgo a las T» = de T-45 a T+duracion+30."""
    assert _w(time_mode="departure", time_at="08:00", duration_min=45) == ("07:15", "09:15")
    assert _w(time_mode="departure", time_at="18:00", duration_min=40) == ("17:15", "19:10")
    # Sin duracion conocida se suponen 60 min.
    assert _w(time_mode="departure", time_at="08:00", duration_min=0) == ("07:15", "09:30")
    # No se sale del dia por arriba.
    assert _w(time_mode="departure", time_at="23:30", duration_min=60) == ("22:45", "23:59")


def test_derive_window_llegada():
    """R38: «llego a las T» = de T-duracion-45 a T+15."""
    assert _w(time_mode="arrival", time_at="09:00", duration_min=45) == ("07:30", "09:15")
    assert _w(time_mode="arrival", time_at="09:00", duration_min=38) == ("07:37", "09:15")
    # «Llego a las 09:00» sin saber cuanto dura: 60 min.
    assert _w(time_mode="arrival", time_at="09:00", duration_min=0) == ("07:15", "09:15")
    # No se sale del dia por abajo.
    assert _w(time_mode="arrival", time_at="00:30", duration_min=45) == ("00:00", "00:45")


def test_derive_window_franja_tal_cual():
    assert _w(time_mode="window") == ("07:00", "10:00")
    assert _w(time_mode="window", time_from="17:30", time_to="19:00") == ("17:30", "19:00")
    # Sin hora, la franja se queda como estaba; un modo raro es franja.
    assert _w(time_mode="arrival", time_at="") == ("07:00", "10:00")
    assert _w(time_mode="cualquiera", time_at="08:00") == ("07:00", "10:00")
    assert B.derive_window({}) == ("07:00", "10:00")


def test_ruta_guarda_modo_hora_duracion_y_franja(env):
    """Lo de tools/test_routes.py: se guarda y se relee tal cual se definio,
    con la franja ya calculada."""
    from app import db
    db.init()
    leg = [{"line_id": "line:IDFM:C01739", "from_id": "stop_area:IDFM:71370"}]
    rid = db.save_route({
        "name": "llego a las 9", "origin_id": "a", "origin_name": "A",
        "dest_id": "b", "dest_name": "B", "days": [0, 1, 2, 3, 4],
        "time_mode": "arrival", "time_at": "09:00", "duration_min": 38, "legs": leg})
    r = db.get_route(rid)
    assert r["time_mode"] == "arrival" and r["time_at"] == "09:00"
    assert r["duration_min"] == 38
    assert (r["time_from"], r["time_to"]) == ("07:37", "09:15")
    # Un modo desconocido se guarda como franja.
    rid2 = db.save_route({"name": "x", "origin_id": "a", "origin_name": "A",
                          "dest_id": "b", "dest_name": "B", "time_mode": "raro",
                          "time_from": "06:00", "time_to": "06:30", "legs": leg})
    r2 = db.get_route(rid2)
    assert r2["time_mode"] == "window" and (r2["time_from"], r2["time_to"]) == ("06:00", "06:30")


def test_migracion_desde_la_primera_version(env):
    """Una BD de la primera version (sin time_mode, time_at ni duration_min)
    se migra sin perder la ruta; migrar dos veces no rompe nada."""
    from app import db
    con = sqlite3.connect(settings.db_path)
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
    INSERT INTO routes (name, origin_id, origin_name, dest_id, dest_name, time_from, time_to)
      VALUES ('generica de antes','a','A','b','B','07:00','22:00');
    """)
    con.commit()
    con.close()

    db.init()
    vieja = db.list_routes()[0]
    assert vieja["name"] == "generica de antes"
    assert vieja["time_mode"] == "window" and vieja["time_at"] == ""
    assert (vieja["time_from"], vieja["time_to"]) == ("07:00", "22:00")
    db.init()
    assert len(db.list_routes()) == 1


# =====================================================================
#  R37: que ruta toca
# =====================================================================

def _r(rid, desde, hasta, days=(0, 1, 2, 3, 4), position=0, name=None) -> dict:
    return {"id": rid, "name": name or f"r{rid}", "days": list(days),
            "time_from": desde, "time_to": hasta, "position": position}


def test_pick_active_route_mas_concreta(env):
    """R37: si encajan varias, gana la franja mas estrecha, no la primera de
    la lista (la generica de 07:00 a 22:00 tapaba a «llego a las 9»)."""
    from app import db
    db.init()
    leg = [{"line_id": "line:IDFM:C01739", "from_id": "stop_area:IDFM:71370"}]
    base = {"origin_id": "a", "origin_name": "A", "dest_id": "b", "dest_name": "B",
            "days": [0, 1, 2, 3, 4], "legs": leg}
    db.save_route({**base, "name": "generica de antes", "time_from": "07:00",
                   "time_to": "22:00"})
    db.save_route({**base, "name": "llego a las 9", "time_mode": "arrival",
                   "time_at": "09:00", "duration_min": 38})
    db.save_route({**base, "name": "salgo a las 18", "time_mode": "departure",
                   "time_at": "18:00", "duration_min": 40})
    todas = db.list_routes()
    lunes = (2026, 8, 31)
    for (h, m), esperada in (((8, 30), "llego a las 9"), ((18, 10), "salgo a las 18"),
                             ((12, 0), "generica de antes")):
        assert B.pick_active_route(todas, _paris(*lunes, h, m))["name"] == esperada

    # A igual anchura, menor posicion y luego menor id.
    iguales = [_r(2, "07:00", "09:00", position=1), _r(3, "08:00", "10:00", position=0)]
    assert B.pick_active_route(iguales, _paris(*lunes, 8, 30))["id"] == 3
    iguales = [_r(5, "07:00", "09:00"), _r(4, "08:00", "10:00")]
    assert B.pick_active_route(iguales, _paris(*lunes, 8, 30))["id"] == 4


def test_pick_active_route_sin_franja():
    """R37 y fallo 18.3.13: sin franja que encaje, la proxima de HOY de verdad
    (la que empieza antes a partir de ahora), no la primera de la lista."""
    lunes = (2026, 8, 31)
    rutas = [_r(1, "07:00", "09:00", name="mañana"),
             _r(2, "19:00", "20:00", name="noche"),
             _r(3, "13:00", "14:00", name="comida"),
             _r(4, "12:00", "13:00", days=[5, 6], name="finde")]
    # A las 10:00 la siguiente es la de las 13:00 aunque en la lista vaya
    # detras de la de las 19:00 (la 0.3.0 cogia la de las 19:00).
    assert B.pick_active_route(rutas, _paris(*lunes, 10, 0))["name"] == "comida"
    assert B.pick_active_route(rutas, _paris(*lunes, 16, 0))["name"] == "noche"
    # Si hoy ya no queda ninguna: la que acabo hace menos.
    assert B.pick_active_route(rutas, _paris(*lunes, 22, 0))["name"] == "noche"
    # Antes de la primera del dia: esa.
    assert B.pick_active_route(rutas, _paris(*lunes, 5, 0))["name"] == "mañana"
    # Sabado: solo la de fin de semana es de hoy.
    assert B.pick_active_route(rutas, _paris(2026, 9, 5, 9, 0))["name"] == "finde"
    # Hoy no hay ninguna: la primera de la lista. Nunca pantalla vacia.
    solo_finde = [_r(7, "12:00", "13:00", days=[5]), _r(8, "08:00", "09:00", days=[6])]
    assert B.pick_active_route(solo_finde, _paris(*lunes, 9, 0))["id"] == 7
    assert B.pick_active_route([], _paris(*lunes, 9, 0)) is None
    # Sin dias guardados no es de ningun dia, pero tampoco rompe nada.
    assert B.pick_active_route([_r(9, "07:00", "09:00", days=[])], _paris(*lunes, 8, 0))["id"] == 9


def test_pick_active_route_cruza_medianoche():
    """R37 y fallo 18.3.13: una franja de 23:00 a 01:00 funciona y la parte
    de despues de medianoche es del dia en que empezo."""
    viernes_noche = _r(1, "23:00", "01:00", days=[4], name="viernes noche")
    manana = _r(2, "07:00", "10:00", days=[0, 1, 2, 3, 4, 5, 6], name="mañana")
    generica = _r(3, "00:00", "23:59", days=[0, 1, 2, 3, 4, 5, 6], name="todo el dia")
    rutas = [generica, manana, viernes_noche]
    # Viernes 23:30 y sabado 00:30: la de la noche (mas estrecha que la generica).
    assert B.pick_active_route(rutas, _paris(2026, 9, 4, 23, 30))["name"] == "viernes noche"
    assert B.pick_active_route(rutas, _paris(2026, 9, 5, 0, 30))["name"] == "viernes noche"
    # El viernes a las 00:30 todavia no: esa madrugada es la del jueves.
    assert B.pick_active_route(rutas, _paris(2026, 9, 4, 0, 30))["name"] == "todo el dia"
    # Sabado a las 01:30 ya se acabo.
    assert B.pick_active_route(rutas, _paris(2026, 9, 5, 1, 30))["name"] == "todo el dia"
    # Sin generica: el viernes a las 22:00 la proxima es la de las 23:00.
    assert B.pick_active_route([manana, viernes_noche],
                               _paris(2026, 9, 4, 22, 0))["name"] == "viernes noche"


# =====================================================================
#  Avisos: R28, R68, R69 y fallo 18.3.19
# =====================================================================

@pytest.mark.parametrize("texto", [
    "Le trafic est interrompu entre Châtillon-Montrouge et Montparnasse.",
    "Trafic interrompu entre Saint-Lazare et Houilles.",
    "Le trafic sera interrompu à partir de 21h.",
    "Le trafic reste interrompu sur la ligne.",
    "RER et Transilien : trafic sont interrompus entre A et B.",
    "Trafic seront interrompus demain entre A et B.",
    "Trafic interrompu jusqu'à 14h entre La Défense et Nanterre.",
    "Interruption de trafic entre Nation et Vincennes.",
    "Interruption du trafic sur la branche.",
    "La ligne est interrompue.",
    "Trafic interrompu sur toute la ligne.",
    "Les trains ne circulent pas entre A et B.",
    "Le RER ne circule plus.",
    "Aucun train entre Paris et Mantes.",
    "Le service est interrompu.",
])
def test_interrumpido_con_copula(texto):
    """R68: «le trafic EST interrompu» y sus variantes cortan la linea (nivel
    2), no solo la perturban. La 13 cortada de verdad el 30/08 salia como
    «perturbada» porque la expresion no admitia la copula."""
    idx = B.index_disruptions(_gm(Msg(["C01383"], texto)))
    st = B.line_status("C01383", idx)
    assert st["level"] == B.INTERRUPTED and st["label"] == "interrumpida", texto


def test_perturbado_no_es_interrumpido():
    idx = B.index_disruptions(_gm(Msg(["C01383"], "Trafic perturbé suite à un incident.")))
    st = B.line_status("C01383", idx)
    assert st["level"] == B.DISRUPTED and st["label"] == "perturbada"
    assert B.line_status("C09999", idx) == {"level": 0, "label": "normal", "messages": [],
                                            "planned": 0}


def test_aviso_caducado_y_futuro():
    """R69: un aviso con ValidUntilTime pasado no cuenta; uno que empieza mas
    adelante va a `planned` y no enciende el semaforo."""
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    futuro = ahora.astimezone(settings.tz).date() + timedelta(days=20)
    meses = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre")
    obras = f"Travaux : le {futuro.day} {meses[futuro.month - 1]}, pas de trains."
    idx = B.index_disruptions(_gm(
        Msg(["C01383"], "Trafic interrompu (ya resuelto).", valid_hours=-1),
        Msg(["C01743"], obras),
        Msg(["C01739"], "Trafic perturbé.", short="Perturbé."),
        now=ahora), now=ahora)
    assert "C01383" not in idx                           # caducado
    e = B.line_status("C01743", idx)
    assert e == {"level": 0, "label": "normal", "messages": [], "planned": 1}
    assert idx["C01743"][0]["planned_from"] == futuro.isoformat()
    j = B.line_status("C01739", idx)
    assert j["level"] == 1 and j["messages"] == ["Perturbé."]    # el mas corto


def test_line_status_planificados_no_cuentan():
    """R28: las obras futuras se cuentan aparte y no encienden la linea; los
    mensajes son como mucho 3 y el nivel es el peor de los activos."""
    msgs = {"C01743": [
        {"text": "a", "severity": 1, "planned_from": None},
        {"text": "b", "severity": 2, "planned_from": None},
        {"text": "c", "severity": 1, "planned_from": None},
        {"text": "d", "severity": 1, "planned_from": None},
        {"text": "obras", "severity": 2, "planned_from": "2026-10-01"},
    ]}
    st = B.line_status("C01743", msgs)
    assert st == {"level": 2, "label": "interrumpida", "messages": ["a", "b", "c"], "planned": 1}
    solo_obras = {"C01743": [{"text": "obras", "severity": 2, "planned_from": "2026-10-01"}]}
    assert B.line_status("C01743", solo_obras)["level"] == 0


def test_avisos_hoy_es_la_fecha_de_paris():
    """Fallo 18.3.19: a la 01:30 de Paris del 24 de septiembre (23:30 UTC del
    23) el aviso «du 24 septembre au 27 septembre» es de HOY. Con la fecha
    UTC salia como obras de mañana y la linea quedaba en verde."""
    ahora = datetime(2026, 9, 23, 23, 30, tzinfo=timezone.utc)
    texto = ("Du 24 septembre au 27 septembre inclus, l'arrêt ne sera pas desservi "
             "à Porte de Saint-Ouen (travaux de modernisation)")
    idx = B.index_disruptions(_gm(Msg(["C01383"], texto), now=ahora), now=ahora)
    assert idx["C01383"][0]["planned_from"] is None
    assert B.line_status("C01383", idx)["level"] == B.DISRUPTED
    # Un dia antes en Paris si es planificado.
    antes = datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)      # 22:00 del 23
    idx = B.index_disruptions(_gm(Msg(["C01383"], texto), now=antes), now=antes)
    assert idx["C01383"][0]["planned_from"] == "2026-09-24"


def test_avisos_con_campos_raros_no_revientan():
    ahora = datetime.now(timezone.utc)
    payload = {"Siri": {"ServiceDelivery": {"GeneralMessageDelivery": [
        None,
        {"InfoMessage": None},
        {"InfoMessage": [
            None, "basura",
            {"ItemIdentifier": "IDFM.C01383", "Content": None},
            {"ItemIdentifier": "IDFM.C01383", "Content": {"Message": None}},
            {"ItemIdentifier": "IDFM.C01383", "Content": {"Message": [
                None, {"MessageText": None}, {"MessageText": {"value": None}}]}},
            {"ItemIdentifier": "IDFM.C01383", "ValidUntilTime": 12345,
             "Content": {"Message": [{"MessageText": [{"value": "Trafic perturbé."}]}]}},
            {"ItemIdentifier": "IDFM.C01739", "ValidUntilTime": "2020-01-01T00:00:00",
             "Content": {"Message": [{"MessageText": {"value": "Viejo, sin zona."}}]}},
        ]},
    ]}}}
    idx = B.index_disruptions(payload, now=ahora)
    assert list(idx) == ["C01383"] and idx["C01383"][0]["text"] == "Trafic perturbé."
    assert B.index_disruptions({}) == {} and B.index_disruptions(None) == {}
    assert B.index_disruptions({"Siri": {"ServiceDelivery": {"GeneralMessageDelivery": None}}}) == {}


# =====================================================================
#  Salidas: R4, R5, R71, R72, R74 y fallos 18.3.11 y 18.3.18
# =====================================================================

def test_salida_pasada_y_minutos():
    """R71: lo que salio hace mas de 1 min no se ensena; los minutos se
    redondean y nunca son negativos; orden por minutos y 4 como mucho."""
    deps = B.extract_departures(_sm(
        Dep("C01739", "Ermont - Eaubonne", 12), Dep("C01739", "Ermont - Eaubonne", -3),
        Dep("C01739", "Ermont - Eaubonne", -0.5), Dep("C01739", "Ermont - Eaubonne", 4.4),
        Dep("C01739", "Ermont - Eaubonne", 4.6), Dep("C01739", "Ermont - Eaubonne", 30),
        Dep("C01739", "Ermont - Eaubonne", 60)), J)
    assert [d["minutes"] for d in deps] == [0, 4, 5, 12]
    todas = B.extract_departures(_sm(*[Dep("C01739", "Ermont - Eaubonne", m)
                                       for m in (-1.5, 1, 2)]), J, limit=60)
    assert [d["minutes"] for d in todas] == [1, 2]


def test_extract_departures_sin_aimed_delay_null():
    """R4: sin hora teorica no hay retraso (null) ni aimed_at; con ella, el
    retraso con signo (tambien el adelanto)."""
    deps = B.extract_departures(_sm(
        Dep("C01739", "Ermont - Eaubonne", 3, aimed_delta=None),
        Dep("C01739", "Ermont - Eaubonne", 6, aimed_delta=2),
        Dep("C01739", "Ermont - Eaubonne", 9, aimed_delta=0),
        Dep("C01739", "Ermont - Eaubonne", 12, aimed_delta=-1)), J)
    assert [d["delay"] for d in deps] == [None, 2, 0, -1]
    assert deps[0]["aimed_at"] == "" and len(deps[1]["aimed_at"]) == 5
    # Llegada solo (fin de linea): se usa la hora de llegada.
    fin = B.extract_departures(_sm(Dep("C01739", "Ermont - Eaubonne", 5, arrival_only=True,
                                       aimed_delta=1)), J)
    assert fin[0]["minutes"] == 5 and fin[0]["delay"] == 1


def test_train_length_short_long():
    """R5: longitud del tren (corto/largo) de VehicleFeatureRef; nada de
    ocupacion."""
    assert B.train_length([{"value": "shortTrain"}]) == "short"
    assert B.train_length({"value": "longTrain"}) == "long"
    assert B.train_length("LONG TRAIN") == "long"
    assert B.train_length([]) is None and B.train_length(None) is None
    deps = B.extract_departures(_sm(
        Dep("C01739", "Ermont - Eaubonne", 3, length="longTrain"),
        Dep("C01739", "Ermont - Eaubonne", 6, length="shortTrain"),
        Dep("C01739", "Ermont - Eaubonne", 9)), J)
    assert [d["length"] for d in deps] == ["long", "short", None]
    assert not any("occup" in k.lower() for d in deps for k in d)


def test_board_platform_new_al_aparecer():
    """R74: platform_new se enciende cuando aparece la via de un viaje que no
    la tenia, una sola vez; si la via cambia a otra no es «nueva»."""
    B._seen_platforms.clear()
    tren = Dep("C01739", "Ermont - Eaubonne", 6, platform=None, jid="SNCF:VJ:1")
    otro = Dep("C01739", "Ermont - Eaubonne", 9, platform="unknown", jid="SNCF:VJ:2")
    antes = B.extract_departures(_sm(tren, otro), J)
    assert [d["platform_new"] for d in antes] == [False, False]
    tren.platform = "21"
    ahora = B.extract_departures(_sm(tren, otro), J)
    assert ahora[0]["platform"] == "21" and ahora[0]["platform_new"] is True
    assert ahora[1]["platform"] is None and ahora[1]["platform_new"] is False
    otra_vez = B.extract_departures(_sm(tren, otro), J)
    assert otra_vez[0]["platform_new"] is False
    tren.platform = "17"                              # cambio de via: no es nueva
    assert B.extract_departures(_sm(tren, otro), J)[0]["platform_new"] is False
    # Visto por primera vez ya con via: tambien se canta.
    nuevo = Dep("C01739", "Ermont - Eaubonne", 12, platform="5", jid="SNCF:VJ:3")
    assert B.extract_departures(_sm(nuevo), J)[0]["platform_new"] is True


def test_extract_departures_remember_false_no_toca_la_memoria():
    """Fallo 18.3.11: con remember=False (el recolector) no se apunta nada, y
    el tablero sigue viendo aparecer la via."""
    B._seen_platforms.clear()
    tren = Dep("C01739", "Ermont - Eaubonne", 6, platform="21", jid="SNCF:VJ:9")
    vista = B.extract_departures(_sm(tren), {**J, "directions": []}, limit=60, remember=False)
    assert vista[0]["platform"] == "21" and vista[0]["platform_new"] is False
    assert B._seen_platforms == {}
    assert B.extract_departures(_sm(tren), J)[0]["platform_new"] is True


def test_direccion_sin_tildes_ni_espacios_raros():
    """R72 en el tablero: el sentido guardado casa aunque SIRI mande otro
    guion, espacios finos o mayusculas; sin sentido se ven todos."""
    sm = _sm(Dep("C01739", "Ermont – Eaubonne", 3),
             Dep("C01739", "Gare Saint-Lazare", 5),
             Dep("C01383", "Ermont - Eaubonne", 7))          # otra linea: fuera
    deps = B.extract_departures(sm, {"line_id": "line:IDFM:C01739",
                                     "directions": ["ERMONT - EAUBONNE", " "]})
    assert [d["minutes"] for d in deps] == [3]
    todas = B.extract_departures(sm, {"line_id": "line:IDFM:C01739", "directions": []})
    assert [d["destination"] for d in todas] == ["Ermont – Eaubonne",
                                                 "Gare Saint-Lazare"]
    assert B.observed_destinations(sm, "line:IDFM:C01739") == [
        "Ermont – Eaubonne", "Gare Saint-Lazare"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _siri_con_nulls() -> dict:
    """Una respuesta de SIRI con todo lo raro que puede mandar: nulls, campos
    sueltos en vez de listas, numeros en vez de textos y basura."""
    now = datetime.now(timezone.utc)
    line = {"value": "STIF:Line::C01739:"}
    visits = [
        {"ItemIdentifier": "item-a", "MonitoredVehicleJourney": {
            "LineRef": line, "TrainNumbers": None, "DestinationName": None,
            "FramedVehicleJourneyRef": None, "VehicleFeatureRef": None,
            "MonitoredCall": {"ExpectedDepartureTime": _iso(now + timedelta(minutes=5)),
                              "DestinationDisplay": [{"value": "Ermont - Eaubonne"}],
                              "AimedDepartureTime": None, "DeparturePlatformName": None,
                              "DepartureStatus": None, "VehicleAtStop": None}}},
        {"MonitoredVehicleJourney": {
            "LineRef": [line], "TrainNumbers": {"TrainNumberRef": None},
            "DestinationName": [{"value": "Ermont - Eaubonne"}],
            "FramedVehicleJourneyRef": {"DatedVehicleJourneyRef": None},
            "MonitoredCall": {"ExpectedDepartureTime": _iso(now + timedelta(minutes=12)),
                              "AimedDepartureTime": 12345,
                              "DeparturePlatformName": {"value": "21"},
                              "VehicleAtStop": "false"}}},
        {"ItemIdentifier": "item-c", "MonitoredVehicleJourney": {
            "LineRef": line, "TrainNumbers": {"TrainNumberRef": [{"value": 135711}]},
            "DestinationName": {"value": "Ermont - Eaubonne"},
            "MonitoredCall": {
                "ExpectedDepartureTime": _iso(now + timedelta(minutes=20)),
                # Sin zona: se toma como UTC.
                "AimedDepartureTime": (now + timedelta(minutes=18)).strftime("%Y-%m-%dT%H:%M:%S"),
                "VehicleAtStop": True}}},
        {"MonitoredVehicleJourney": {"LineRef": line, "MonitoredCall": None}},
        {"MonitoredVehicleJourney": {"LineRef": line,
                                     "MonitoredCall": {"ExpectedDepartureTime": "mañana"}}},
        {"MonitoredVehicleJourney": None},
        None, "basura", 7,
    ]
    return {"Siri": {"ServiceDelivery": {"StopMonitoringDelivery": [
        {"MonitoredStopVisit": visits}, None, {"MonitoredStopVisit": None}]}}}


def test_train_numbers_null_no_revienta():
    """Fallo 18.3.18: `TrainNumbers: null` (y otros null de SIRI) no tumban
    el tablero: la visita sale sin ese dato."""
    B._seen_platforms.clear()
    deps = B.extract_departures(_siri_con_nulls(), J)
    assert [d["minutes"] for d in deps] == [5, 12, 20]
    a, b, c = deps
    assert a["train"] is None and a["jid"] == "item-a" and a["status"] == ""
    assert a["at_stop"] is False and a["length"] is None and a["delay"] is None
    assert b["train"] is None and b["platform"] == "21" and b["jid"] == ""
    assert b["aimed_at"] == "" and b["at_stop"] is False
    assert c["train"] == "135711" and c["delay"] == 2 and c["at_stop"] is True
    assert B.observed_destinations(_siri_con_nulls(), "line:IDFM:C01739") == ["Ermont - Eaubonne"]
    for raro in ({}, None, {"Siri": None}, {"Siri": {"ServiceDelivery": {
            "StopMonitoringDelivery": "x"}}}):
        assert B.extract_departures(raro, J) == []


def test_tablero_con_nulls_de_siri_responde(client, fake_prim, make_route):
    """Lo mismo por la API: 200 con la forma de siempre, no un 500."""
    fake_prim._stop_monitoring = lambda ref: _siri_con_nulls()
    fake_prim._general_message = lambda: {"Siri": {"ServiceDelivery": {
        "GeneralMessageDelivery": [{"InfoMessage": [{"Content": None}, None]}]}}}
    make_route()
    r = client.get("/api/board", params={"log_history": "false"})
    assert r.status_code == 200, r.text
    body = r.json()
    _board_030().validate(body)
    assert [d["minutes"] for d in body["legs"][0]["departures"]] == [5, 12, 20]
    assert body["errors"] == [] and body["legs"][0]["status"]["level"] == 0


# =====================================================================
#  Historial: R40 y R82
# =====================================================================

def _tablero_minimo() -> dict:
    return {"legs": [{"line_code": "J", "status": {"label": "normal"},
                      "departures": [{"minutes": 4, "platform": "21"},
                                     {"minutes": 19, "platform": None}]}],
            "worst_level": 0, "max_delay": 2.0, "worst_line": "J"}


def test_record_history_10min(env, make_route):
    """R82: como mucho una fila de historial cada 10 min por ruta."""
    from app import db
    rid = make_route()
    otra = make_route(ruta_j(name="otra"))
    route = db.get_route(rid)
    assert B.record_history(route, _tablero_minimo()) is True
    assert B.record_history(route, _tablero_minimo()) is False
    assert B.record_history(db.get_route(otra), _tablero_minimo()) is True   # otra ruta
    # Hace 11 minutos: ya toca otra.
    hace = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(timespec="seconds")
    with db.conn() as c:
        c.execute("UPDATE history SET ts=? WHERE route_id=?", (hace, rid))
    assert B.record_history(route, _tablero_minimo()) is True
    with db.conn() as c:
        filas = c.execute("SELECT * FROM history WHERE route_id=? ORDER BY id", (rid,)).fetchall()
    assert len(filas) == 2
    detalle = json.loads(filas[-1]["detail"])
    assert detalle == {"legs": [{"line": "J", "status": "normal", "next": [4, 19],
                                 "platforms": ["21", None]}]}
    assert filas[-1]["day"] == datetime.now(settings.tz).strftime("%Y-%m-%d")
    assert filas[-1]["delay_min"] == 2.0 and filas[-1]["worst_line"] == "J"


def test_board_log_history_false(client, fake_prim, make_route):
    """R40: los refrescos automaticos (log_history=false) no engordan el
    historial; las consultas de verdad si, una cada 10 min."""
    from app import db
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4))
    make_route()

    def filas() -> int:
        with db.conn() as c:
            return c.execute("SELECT COUNT(*) FROM history").fetchone()[0]

    for _ in range(3):
        assert client.get("/api/board", params={"log_history": "false"}).status_code == 200
    assert filas() == 0
    client.get("/api/board")
    client.get("/api/board")
    assert filas() == 1


# =====================================================================
#  Resiliencia (lo que comprobaba tools/test_resilience.py)
# =====================================================================

@pytest.fixture
async def pc(env, fake_prim):
    from app import db, prim
    db.init()
    await prim.startup()
    yield prim.get_client()
    await prim.shutdown()


async def test_resiliencia_cache_y_cuota_de_la_cabecera(pc, fake_prim):
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4))
    d1, age1 = await pc.stop_monitoring(SL)
    assert fake_prim.calls["stop-monitoring"] == 1          # llega a la API
    assert "StopMonitoringDelivery" in d1["Siri"]["ServiceDelivery"]
    assert age1 == 0.0                                     # recien traido
    d2, age2 = await pc.stop_monitoring(SL)
    assert fake_prim.calls["stop-monitoring"] == 1 and d2 == d1    # de la cache
    assert 0 <= age2 < 5
    # La cuota que queda sale de la cabecera de PRIM.
    assert pc.quota["stop-monitoring"] == fake_prim.remaining["stop-monitoring"]


async def test_resiliencia_api_caida_copia_con_edad_y_se_recupera(pc, fake_prim):
    from app.prim import PrimError
    tren = Dep("C01739", "Ermont - Eaubonne", 4)
    fake_prim.add("71370", tren)
    d1, _ = await pc.stop_monitoring(SL)

    # La API se cae: sigue saliendo el ultimo dato, con su edad REAL.
    fake_prim.fail["*"] = 500
    pc._cache[f"sm:{SL}"].fetched_at -= 90
    d3, age3 = await pc.stop_monitoring(SL)
    assert d3 == d1 and 89 <= age3 <= 92
    assert pc.last_error is not None and "HTTP 500" in pc.last_error

    # Sin dato previo y con la API caida: PrimError, no un dato inventado.
    with pytest.raises(PrimError):
        await pc.stop_monitoring("STIF:StopArea:SP:99999:")

    # Se recupera sola (pasada la pausa tras el fallo): dato fresco, edad 0 y
    # el error se borra.
    del fake_prim.fail["*"]
    tren.minutes = 9
    reloj = pc._clock
    pc._clock = lambda: reloj() + 3600
    pc._cache[f"sm:{SL}"].fetched_at -= 90
    d4, age4 = await pc.stop_monitoring(SL)
    assert d4 != d1 and age4 == 0.0
    assert pc.last_error is None


async def test_resiliencia_simultaneas_una_sola_llamada(pc, fake_prim):
    fake_prim.delay = 0.05
    await asyncio.gather(*[pc.stop_monitoring("STIF:StopArea:SP:12345:") for _ in range(5)])
    assert fake_prim.calls["stop-monitoring"] == 1


async def test_resiliencia_tramo_roto_no_tumba_el_tablero(pc, fake_prim):
    fake_prim.fail["*"] = "timeout"
    route = {"id": 1, "name": "prueba", "origin_name": "A", "dest_name": "B",
             "legs": [{"seq": 0, "line_id": "line:IDFM:C01739", "line_code": "J",
                       "line_name": "J", "line_mode": "Train", "line_color": "CEC73D",
                       "from_id": "stop_area:IDFM:71370", "from_name": "Saint-Lazare",
                       "to_id": "", "to_name": "", "directions": []}]}
    t0 = time.monotonic()
    board = await B.build_board(route)
    assert time.monotonic() - t0 < 5
    assert isinstance(board, dict) and len(board["legs"]) == 1
    assert board["legs"][0]["departures"] == [] and board["legs"][0]["age"] is None
    assert board["errors"] and board["_all_failed"] is True
