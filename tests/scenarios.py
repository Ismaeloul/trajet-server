"""Escenarios del PRIM falso: los casos de PreviewData de la app, de verdad.

Cada funcion prepara el PRIM falso (y la BD si hace falta) para un caso de
`Resources/PreviewData.swift` (docs/inventario-funcional.md §17, F77-F79) y
devuelve un `Caso` con la ruta que hay que guardar. Los tests los pasan por
el tablero de verdad (/api/board): asi lo que la app pinta en sus vistas
previas es lo que el servidor manda de verdad, no una maqueta.

    caso = scenarios.via_que_aparece(fake_prim)
    client.post("/api/routes", json=caso.ruta)
    client.get("/api/board")          # sin via
    caso.paso2()                      # PRIM publica la via
    client.get("/api/board")          # via nueva: platform_new

Lineas y estaciones: los codigos de linea son los reales de IDFM; Saint-Lazare
(71370) y Argenteuil (65063) tambien. Las paradas de bus y tranvia y la del
RER E llevan codigos inventados con la forma real (490001...).

El reloj del PRIM falso se fija al crear el caso: asi las horas teoricas que
se siembran en `platform_obs` casan al minuto con las que manda el tablero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from fakeprim import Dep, FakePrim, Msg

PARIS = ZoneInfo("Europe/Paris")

# (line_id, codigo, modo, color): codigos reales de IDFM.
J = ("line:IDFM:C01739", "J", "Train", "CEC73D")
RER_E = ("line:IDFM:C01743", "E", "RER", "B94E9A")
M13 = ("line:IDFM:C01383", "13", "Métro", "82C8E6")
T2 = ("line:IDFM:C01390", "T2", "Tramway", "C4318E")
BUS_6424 = ("line:IDFM:C00306", "6424", "Bus", "A50034")
BUS_147 = ("line:IDFM:C01199", "147", "Bus", "E4022D")

# (zdc, nombre)
SAINT_LAZARE = ("71370", "Gare Saint-Lazare")
ARGENTEUIL = ("65063", "Argenteuil")
MARSEILLAISE = ("490001", "Marseillaise")
PONT_BEZONS = ("490011", "Pont de Bezons")
PARC_BEZONS = ("490002", "Parc de Bezons")
PORTE_VERSAILLES = ("490012", "Porte de Versailles")
HAUSSMANN = ("490003", "Haussmann Saint-Lazare")
CHELLES = ("490013", "Chelles - Gournay")
EGLISE_PANTIN = ("490004", "Église de Pantin")
GALLIENI = ("490014", "Gallieni")
VICTOR_BASCH = ("490005", "Victor Basch")

# Textos de aviso tal cual los escribe IDFM (en frances).
AVISO_13_CORTADA = ("Le trafic est interrompu entre Châtillon-Montrouge et Montparnasse "
                    "suite à un incident technique.")
AVISO_147 = "Trafic ralenti en raison de travaux sur la voirie."
AVISO_147_ES = "Tráfico lento por obras en la calzada."

MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre")


@dataclass
class Caso:
    nombre: str
    ruta: dict
    fake: FakePrim
    # Segundo paso del caso (la via que aparece). Cambia el mundo del PRIM
    # falso y vacia la cache del cliente para que el siguiente tablero lo vea.
    paso2: Callable[[], None] | None = None
    notas: dict = field(default_factory=dict)


# ---------------- piezas ----------------

def tramo(linea: tuple, desde: tuple, hasta: tuple = ("", ""),
          directions: list[str] | tuple = ()) -> dict:
    lid, code, mode, color = linea
    return {
        "line_id": lid, "line_code": code, "line_name": code, "line_mode": mode,
        "line_color": color,
        "from_id": f"stop_area:IDFM:{desde[0]}", "from_name": desde[1],
        "to_id": f"stop_area:IDFM:{hasta[0]}" if hasta[0] else "", "to_name": hasta[1],
        "directions": list(directions),
    }


def ruta(nombre: str, tramos: list[dict], origen: tuple | None = None,
         destino: tuple | None = None) -> dict:
    """Ruta de todos los dias y todo el dia: siempre es la que toca."""
    o = origen or (tramos[0]["from_id"], tramos[0]["from_name"])
    d = destino or (tramos[-1]["to_id"] or tramos[-1]["from_id"], tramos[-1]["to_name"])
    return {"name": nombre, "origin_id": o[0], "origin_name": o[1],
            "dest_id": d[0], "dest_name": d[1],
            "days": [0, 1, 2, 3, 4, 5, 6], "time_from": "00:00", "time_to": "23:59",
            "legs": tramos}


def fijar_reloj(fp: FakePrim) -> datetime:
    """Congela el 'ahora' del PRIM falso (al segundo) y lo devuelve."""
    if fp.now is None:
        fp.now = datetime.now(timezone.utc).replace(microsecond=0)
    return fp.now


def hhmm_paris(dt: datetime) -> str:
    return dt.astimezone(PARIS).strftime("%H:%M")


def _vaciar_cache() -> None:
    from app import prim
    if prim.client is not None:
        prim.get_client().clear_cache()


def _db():
    from app import db
    db.init()
    return db


def sembrar_andenes(stop_id: str, line_id: str, dest: str, platforms: dict[str, int],
                    train: str = "", aimed: str = "", weekday: int | None = None,
                    semanal: bool = False) -> int:
    """Mete observaciones de via de dias anteriores (una por dia) para que la
    prevision se atreva: {"11": 14, "7": 3} = 14 dias por la 11 y 3 por la 7.
    `semanal` las pone en el mismo dia de la semana que hoy (para la
    prevision por hora, que separa laborables y fin de semana)."""
    from app.idfm import norm_text
    db = _db()
    hoy = datetime.now(PARIS).date()
    wd = hoy.weekday() if weekday is None else weekday
    filas = []
    n = 0
    for platform, veces in platforms.items():
        for _ in range(veces):
            n += 1
            dia = hoy - timedelta(days=7 * n if semanal else n)
            filas.append((dia.isoformat(), f"{dia.isoformat()}T08:00:00+02:00", stop_id,
                          line_id, norm_text(dest), train, aimed,
                          dia.weekday() if semanal else wd, platform))
    with db.conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO platform_obs "
            "(day, seen_at, stop_id, line_id, dest, train, aimed, weekday, platform) "
            "VALUES (?,?,?,?,?,?,?,?,?)", filas)
    return len(filas)


def aviso_futuro(dias: int = 20) -> str:
    """Obras dentro de `dias` dias: cuenta en `planned`, no enciende la linea."""
    d = date.today() + timedelta(days=dias)
    return (f"Travaux : le {d.day} {MOIS[d.month - 1]}, pas de trains entre "
            "Haussmann Saint-Lazare et Magenta.")


# ---------------- casos de PreviewData ----------------

def sin_via(fp: FakePrim) -> Caso:
    """La J en Saint-Lazare: PRIM manda 'unknown' en la via (R2, R73)."""
    fijar_reloj(fp)
    fp.add(SAINT_LAZARE[0],
           Dep("C01739", "Ermont - Eaubonne", 6, platform="unknown", train="137412",
               length="longTrain"),
           Dep("C01739", "Ermont - Eaubonne", 21, platform="unknown", train="137416"))
    return Caso("sin_via", ruta("Trabajo → Casa", [
        tramo(J, SAINT_LAZARE, ARGENTEUIL, ["Ermont - Eaubonne"])]), fp)


def via_que_aparece(fp: FakePrim) -> Caso:
    """Dos llamadas: la primera sin via y la segunda con la 21 -> platform_new."""
    fijar_reloj(fp)
    tren = Dep("C01739", "Ermont - Eaubonne", 5, platform=None, train="135711",
               jid="SNCF:VJ:J:135711", length="longTrain")
    fp.add(SAINT_LAZARE[0], tren,
           Dep("C01739", "Ermont - Eaubonne", 20, platform=None, train="135715",
               jid="SNCF:VJ:J:135715"))

    def paso2():
        tren.platform = "21"
        _vaciar_cache()

    return Caso("via_que_aparece", ruta("Trabajo → Casa", [
        tramo(J, SAINT_LAZARE, ARGENTEUIL, ["Ermont - Eaubonne"])]), fp, paso2)


def _rer_e(fp: FakePrim) -> tuple[Dep, dict]:
    """RER E en Haussmann: e1 con via real, e2 con prevision por mision (82 %)
    y e3 con prevision por hora (5 de 9). Siembra la BD."""
    now = fijar_reloj(fp)
    e1 = Dep("C01743", "Chelles - Gournay", 4, aimed_delta=2, platform=None,
             train="135711", jid="SNCF:VJ:E:135711", length="longTrain")
    e2 = Dep("C01743", "Chelles - Gournay", 12, aimed_delta=0, train="135713",
             jid="SNCF:VJ:E:135713", length="shortTrain")
    e3 = Dep("C01743", "Chelles - Gournay", 22, aimed_delta=None, train="135715",
             jid="SNCF:VJ:E:135715")
    fp.add(HAUSSMANN[0], e1, e2, e3)
    stop = f"stop_area:IDFM:{HAUSSMANN[0]}"
    sembrar_andenes(stop, RER_E[0], "Chelles - Gournay", {"11": 14, "7": 3},
                    train="135713", aimed=hhmm_paris(now + timedelta(minutes=12)))
    # Sin hora teorica, la prevision usa la prevista (d["at"]).
    hora_e3 = hhmm_paris(now + timedelta(minutes=22))
    sembrar_andenes(stop, RER_E[0], "Chelles - Gournay", {"7": 5, "11": 4},
                    aimed=hora_e3, semanal=True)
    return e1, {"hora_e3": hora_e3}


def via_probable(fp: FakePrim) -> Caso:
    """Via real frente a via solo probable (R10): la 11 de verdad en el
    primero, 'probable 11' (mision, 82 %) y 'probable 7' (hora, 56 %)."""
    e1, notas = _rer_e(fp)
    e1.platform = "11"
    return Caso("via_probable", ruta("Al trabajo", [
        tramo(RER_E, HAUSSMANN, CHELLES, ["Chelles - Gournay"])]), fp, notas=notas)


def bus_106(fp: FakePrim) -> Caso:
    """Bus 6424 con hora teorica: +11 (delayed) y un paso a 106 min (R6)."""
    fijar_reloj(fp)
    fp.add(MARSEILLAISE[0],
           Dep("C00306", "Pont de Bezons", 6, aimed_delta=11, status="delayed"),
           Dep("C00306", "Pont de Bezons", 56, aimed_delta=1),
           Dep("C00306", "Pont de Bezons", 106, aimed_delta=1))
    return Caso("bus_106", ruta("Casa → Bezons", [
        tramo(BUS_6424, MARSEILLAISE, PONT_BEZONS, ["Pont de Bezons"])]), fp)


def bus_165(fp: FakePrim) -> Caso:
    """Pasos a 60 min ('1h') y 165 min ('2h45') (F78, F79)."""
    fijar_reloj(fp)
    fp.add(MARSEILLAISE[0],
           Dep("C00306", "Pont de Bezons", 60, aimed_delta=0),
           Dep("C00306", "Pont de Bezons", 165, aimed_delta=0))
    return Caso("bus_165", ruta("Casa → Bezons", [
        tramo(BUS_6424, MARSEILLAISE, PONT_BEZONS, ["Pont de Bezons"])]), fp)


def linea_cortada(fp: FakePrim) -> Caso:
    """La 13 cortada ('le trafic est interrompu') y sin salidas: tramo vacio
    que la app pinta como «Sin circulación» (R25, R68)."""
    fijar_reloj(fp)
    fp.message(Msg(["C01383"], AVISO_13_CORTADA))
    return Caso("linea_cortada", ruta("Metro", [
        tramo(M13, SAINT_LAZARE, ("", ""), [])]), fp)


def aviso_frances(fp: FakePrim) -> Caso:
    """Bus 147 perturbado con un aviso en frances aun sin traducir (R8)."""
    fijar_reloj(fp)
    fp.add(EGLISE_PANTIN[0],
           Dep("C01199", "Gallieni - Pont de Bondy", 2, aimed_delta=None),
           Dep("C01199", "Gallieni - Pont de Bondy", 27, aimed_delta=None))
    fp.message(Msg(["C01199"], AVISO_147))
    return Caso("aviso_frances", ruta("Pantin", [
        tramo(BUS_147, EGLISE_PANTIN, GALLIENI, ["Gallieni - Pont de Bondy"])]), fp)


def tren_en_anden(fp: FakePrim) -> Caso:
    """Metro 13 parado en el anden: 0 min y at_stop (R15)."""
    fijar_reloj(fp)
    fp.add(SAINT_LAZARE[0],
           Dep("C01383", "Châtillon Montrouge", 0, aimed_delta=None, at_stop=True),
           Dep("C01383", "Châtillon Montrouge", 4, aimed_delta=None))
    return Caso("tren_en_anden", ruta("Metro", [
        tramo(M13, SAINT_LAZARE, ("", ""), ["Châtillon Montrouge"])]), fp)


def destinos_mezclados(fp: FakePrim) -> Caso:
    """Tramo sin sentido con dos destinos: cada salida dice a donde va (R24).
    La J de la misma estacion no se cuela."""
    fijar_reloj(fp)
    fp.add(SAINT_LAZARE[0],
           Dep("C01383", "Saint-Denis Université", 1, aimed_delta=None),
           Dep("C01383", "Asnières-Gennevilliers Les Courtilles", 3, aimed_delta=None),
           Dep("C01383", "Saint-Denis Université", 9, aimed_delta=None),
           Dep("C01739", "Ermont - Eaubonne", 5))
    return Caso("destinos_mezclados", ruta("Metro", [
        tramo(M13, SAINT_LAZARE, ("", ""), [])]), fp)


def tramo_vacio(fp: FakePrim) -> Caso:
    """T2 sin salidas y sin avisos: «Servicio finalizado» (R25)."""
    fijar_reloj(fp)
    return Caso("tramo_vacio", ruta("Tranvía", [
        tramo(T2, PARC_BEZONS, PORTE_VERSAILLES, ["Porte de Versailles"])]), fp)


def salidas_pasadas(fp: FakePrim) -> Caso:
    """Una salida de hace 3 min (fuera) y otra de hace 30 s (0 min) (R71)."""
    fijar_reloj(fp)
    fp.add(SAINT_LAZARE[0],
           Dep("C01739", "Ermont - Eaubonne", -3),
           Dep("C01739", "Ermont - Eaubonne", -0.5),
           Dep("C01739", "Ermont - Eaubonne", 4))
    return Caso("salidas_pasadas", ruta("Trabajo → Casa", [
        tramo(J, SAINT_LAZARE, ARGENTEUIL, ["Ermont - Eaubonne"])]), fp)


def estacion_caida(fp: FakePrim) -> Caso:
    """Dos estaciones y una no responde: el tablero sale igual y el fallo
    va al pie, «Victor Basch: tiempo de espera agotado» (R26)."""
    fijar_reloj(fp)
    fp.add(SAINT_LAZARE[0], Dep("C01739", "Ermont - Eaubonne", 4))
    fp.add(VICTOR_BASCH[0], Dep("C00306", "Pont de Bezons", 7))
    fp.fail_stations[VICTOR_BASCH[0]] = "timeout"
    return Caso("estacion_caida", ruta("Dos estaciones", [
        tramo(J, SAINT_LAZARE, ARGENTEUIL, ["Ermont - Eaubonne"]),
        tramo(BUS_6424, VICTOR_BASCH, PONT_BEZONS, ["Pont de Bezons"])]), fp)


def casa_trabajo(fp: FakePrim) -> Caso:
    """El caso dificil: «Casa → Trabajo» con 5 tramos (PreviewData.fiveLegBoard).

    0 bus 6424 con +11 y un paso a 106 min · 1 T2 vacio y normal · 2 RER E con
    la via que aparece, vias probables, longitudes y obras futuras · 3 metro
    13 cortado, aviso sin traducir, tren en el anden y destinos mezclados ·
    4 bus 147 perturbado con el aviso ya traducido.
    """
    from app import translate
    bus = bus_106(fp)
    e1, notas = _rer_e(fp)
    fp.add(SAINT_LAZARE[0],
           Dep("C01383", "Saint-Denis Université", 0, aimed_delta=None, at_stop=True),
           Dep("C01383", "Asnières-Gennevilliers Les Courtilles", 3, aimed_delta=None),
           Dep("C01383", "Saint-Denis Université", 9, aimed_delta=None))
    fp.add(EGLISE_PANTIN[0],
           Dep("C01199", "Gallieni - Pont de Bondy", 2, aimed_delta=None),
           Dep("C01199", "Gallieni - Pont de Bondy", 27, aimed_delta=None))
    fp.message(Msg(["C01383"], AVISO_13_CORTADA),
               Msg(["C01199"], AVISO_147),
               Msg(["C01743"], aviso_futuro()))
    _db()
    translate.store(AVISO_147, AVISO_147_ES)

    def paso2():
        e1.platform = "11"
        _vaciar_cache()

    tramos = [
        bus.ruta["legs"][0],
        tramo(T2, PARC_BEZONS, PORTE_VERSAILLES, ["Porte de Versailles"]),
        tramo(RER_E, HAUSSMANN, CHELLES, ["Chelles - Gournay"]),
        tramo(M13, SAINT_LAZARE, ("", ""), []),
        tramo(BUS_147, EGLISE_PANTIN, GALLIENI, ["Gallieni - Pont de Bondy"]),
    ]
    return Caso("casa_trabajo", ruta(
        "Casa → Trabajo", tramos,
        origen=("2.2170;48.9270", "6 Rue de la Marseillaise"),
        destino=("2.4040;48.8930", "74 Rue de Paris")), fp, paso2, notas)


# ---------------- errores de PRIM ----------------

FALLOS = (401, 403, 429, 500, "timeout")


def error_prim(fp: FakePrim, fallo: int | str) -> Caso:
    """PRIM entero responde con `fallo` (401, 403, 429, 500 o "timeout").

    El 429 es el de la cuota del dia: la cabecera llega a 0, como cuando se
    agota de verdad (con cuota aun en la cabecera seria un 429 de rafaga).
    """
    fijar_reloj(fp)
    fp.add(SAINT_LAZARE[0], Dep("C01739", "Ermont - Eaubonne", 4))
    fp.fail["*"] = fallo
    if fallo == 429:
        for ep in fp.remaining:
            fp.remaining[ep] = 1
    return Caso(f"error_{fallo}", ruta("Trabajo → Casa", [
        tramo(J, SAINT_LAZARE, ARGENTEUIL, ["Ermont - Eaubonne"])]), fp)


# Casos que solo necesitan el PRIM falso, por nombre.
ESCENARIOS: dict[str, Callable[[FakePrim], Caso]] = {
    "sin_via": sin_via,
    "via_que_aparece": via_que_aparece,
    "via_probable": via_probable,
    "bus_106": bus_106,
    "bus_165": bus_165,
    "linea_cortada": linea_cortada,
    "aviso_frances": aviso_frances,
    "tren_en_anden": tren_en_anden,
    "destinos_mezclados": destinos_mezclados,
    "tramo_vacio": tramo_vacio,
    "salidas_pasadas": salidas_pasadas,
    "estacion_caida": estacion_caida,
    "casa_trabajo": casa_trabajo,
}
