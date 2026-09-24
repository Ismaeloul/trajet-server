"""Planificador y alternativas (app/planner.py y app/api/common.py): R33,
R34, R79 y R81, y las coordenadas de Navitia que se guardan con la ruta para
el mapa.

Incluye lo que hacian tools/test_plan.py y tools/test_arrival.py contra la
API real (gastando cuota de Navitia), ahora contra el PRIM falso: buscar una
direccion y una parada en el mismo buscador, planificar de una direccion a
una parada, «salir a» frente a «llegar a», guardar la opcion elegida como
ruta, ver su tablero y borrarla.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import pytest
import scenarios as S
from _seg_contrato import schema_validator
from conftest import ruta_j
from fakeprim import Dep, Msg

from app import planner
from app.config import settings

HERE = os.path.dirname(os.path.abspath(__file__))

J = S.J
M14 = ("line:IDFM:C01384", "14", "Métro", "62259D")
BUS_272 = ("line:IDFM:C01272", "272", "Bus", "8C6E2E")

# (zdc, nombre, lat, lon) del punto de parada de cada linea
SL_J = ("71370", "Gare Saint-Lazare", 48.874669, 2.325170)
ARG = ("65063", "Argenteuil", 48.948169, 2.255212)
SL_14 = ("71370", "Saint-Lazare", 48.875600, 2.325400)
MADELEINE = ("71300", "Madeleine", 48.870000, 2.325000)
PONT_BEZONS = ("490011", "Pont de Bezons", 48.920000, 2.215000)
ADDR = "2.35995;48.855602"              # 12 Rue de Rivoli (id de Navitia = lon;lat)


@pytest.fixture(autouse=True)
def sin_coordenadas():
    planner._coords.clear()
    yield
    planner._coords.clear()


def _punto(p: tuple) -> dict:
    zdc, name, lat, lon = p
    return {"id": f"stop_point:IDFM:{zdc}9", "name": name, "embedded_type": "stop_point",
            "stop_point": {"id": f"stop_point:IDFM:{zdc}9", "name": name,
                           "coord": {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}"},
                           "stop_area": {"id": f"stop_area:IDFM:{zdc}", "name": name,
                                         "coord": {"lat": f"{lat + 0.001:.6f}",
                                                   "lon": f"{lon + 0.001:.6f}"}}}}


def _pt(linea: tuple, desde: tuple, hasta: tuple, sale: str, minutos: int,
        direccion: str) -> dict:
    lid, code, mode, color = linea
    return {"type": "public_transport", "duration": minutos * 60,
            "departure_date_time": sale,
            "display_informations": {"code": code, "label": code, "name": code,
                                     "commercial_mode": mode, "color": color,
                                     "direction": direccion},
            "links": [{"type": "line", "id": lid}],
            "from": _punto(desde), "to": _punto(hasta)}


def _a_pie(minutos: int, tipo: str = "street_network") -> dict:
    return {"type": tipo, "duration": minutos * 60}


def _viaje(sale: str, llega: str, minutos: int, transbordos: int, *secciones,
           tipo: str = "best") -> dict:
    return {"type": tipo, "duration": minutos * 60, "nb_transfers": transbordos,
            "departure_date_time": f"20260924T{sale.replace(':', '')}00",
            "arrival_date_time": f"20260924T{llega.replace(':', '')}00",
            "sections": list(secciones)}


def _t(hhmm: str) -> str:
    return f"20260924T{hhmm.replace(':', '')}00"


def _salir_a_las_9() -> dict:
    """Itinerarios «salir a las 09:00» desde casa hasta Argenteuil."""
    return {"journeys": [
        _viaje("09:01", "09:41", 40, 1, _a_pie(4),
               _pt(M14, MADELEINE, SL_14, _t("09:05"), 3, "Saint-Denis Pleyel"),
               _a_pie(4, "transfer"), _a_pie(2, "waiting"),
               _pt(J, SL_J, ARG, _t("09:14"), 21, "Ermont - Eaubonne (Eaubonne)"),
               _a_pie(5)),
        _viaje("09:10", "10:05", 55, 0, _a_pie(8),
               _pt(BUS_272, MADELEINE, PONT_BEZONS, _t("09:18"), 47, "Pont de Bezons")),
        _viaje("09:04", "09:35", 31, 0, _a_pie(5, "crow_fly"),
               _pt(J, SL_J, ARG, _t("09:09"), 21, "Ermont - Eaubonne (Eaubonne)"),
               _a_pie(5)),
        # La misma J un poco mas tarde: Navitia repite casi iguales.
        _viaje("09:19", "09:50", 31, 1, _a_pie(5),
               _pt(J, SL_J, ARG, _t("09:24"), 21, "Ermont - Eaubonne (Eaubonne)"),
               _a_pie(5)),
        # Andando entero: no se puede vigilar.
        _viaje("09:00", "11:36", 156, 0, _a_pie(156), tipo="non_pt_walk"),
    ]}


def _llegar_a_las_9() -> dict:
    """Itinerarios «llegar a las 09:00» (Navitia resuelve hacia atras)."""
    return {"journeys": [
        _viaje("07:50", "08:55", 65, 0, _a_pie(8),
               _pt(BUS_272, MADELEINE, PONT_BEZONS, _t("07:58"), 57, "Pont de Bezons")),
        _viaje("08:05", "08:37", 32, 0, _a_pie(5),
               _pt(J, SL_J, ARG, _t("08:10"), 21, "Ermont - Eaubonne (Eaubonne)"), _a_pie(6)),
        _viaje("08:20", "08:52", 32, 0, _a_pie(5),
               _pt(J, SL_J, ARG, _t("08:25"), 21, "Ermont - Eaubonne (Eaubonne)"), _a_pie(6)),
        _viaje("08:12", "08:50", 38, 1, _a_pie(4),
               _pt(M14, MADELEINE, SL_14, _t("08:16"), 3, "Saint-Denis Pleyel"),
               _a_pie(4, "transfer"),
               _pt(J, SL_J, ARG, _t("08:25"), 21, "Ermont - Eaubonne (Eaubonne)"), _a_pie(4)),
    ]}


def _navitia(fake_prim, salida: dict, llegada: dict | None = None) -> list[dict]:
    """El PRIM falso contesta a /journeys segun datetime_represents; devuelve
    la lista de parametros de cada /journeys que le llega."""
    pedidas: list[dict] = []
    original = fake_prim._navitia

    def responder(path, q):
        if "/journeys" in path:
            pedidas.append(q)
            modo = (q.get("datetime_represents") or ["departure"])[0]
            return llegada if (modo == "arrival" and llegada) else salida
        return original(path, q)

    fake_prim._navitia = responder
    return pedidas


def _en_vivo(fake_prim):
    """Lo que circula ahora en Saint-Lazare (para traducir el sentido)."""
    S.fijar_reloj(fake_prim)
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4, platform="21", train="1"),
                  Dep("C01739", "Gare Saint-Lazare", 7),
                  Dep("C01384", "Saint-Denis Pleyel", 2, aimed_delta=None))
    fake_prim.add("71300", Dep("C01272", "Pont de Bezons", 5, aimed_delta=None),
                  Dep("C01384", "Saint-Denis Pleyel", 3, aimed_delta=None))


# =====================================================================
#  parse_journeys y when_param: R81
# =====================================================================

def test_parse_journeys_orden_a_pie_y_repetidos():
    """R81: los itinerarios solo a pie se descartan; por salida, el mas
    rapido primero (a igualdad, menos transbordos); uno por combinacion de
    lineas. A pie cuenta street_network, crow_fly y transfer (no la espera)."""
    opts = planner.parse_journeys(_salir_a_las_9())
    assert [(o["minutes"], o["transfers"]) for o in opts] == [(31, 0), (40, 1), (55, 0)]
    assert [[leg["line_code"] for leg in o["legs"]] for o in opts] == [["J"], ["14", "J"], ["272"]]
    rapido, transbordo, bus = opts
    assert rapido["walk_minutes"] == 10 and transbordo["walk_minutes"] == 13
    assert rapido["departure"] == "09:04" and rapido["arrival"] == "09:35"
    assert rapido["kind"] == "best"
    assert rapido["legs"][0] == {
        "line_id": "line:IDFM:C01739", "line_code": "J", "line_name": "J",
        "line_mode": "Train", "line_color": "CEC73D",
        "from_id": "stop_area:IDFM:71370", "from_name": "Gare Saint-Lazare",
        "to_id": "stop_area:IDFM:65063", "to_name": "Argenteuil",
        "direction": "Ermont - Eaubonne (Eaubonne)", "minutes": 21, "at": "09:09"}
    for o in opts:
        schema_validator("PlanOption").validate(o)
    assert planner.parse_journeys({}) == [] and planner.parse_journeys({"journeys": None}) == []


def test_plan_arrival_prefiere_salir_tarde(client, fake_prim):
    """R34 y R81: «llegar a las 09:00» no es «salir a las 09:00» reordenado:
    se le pide a Navitia hacia atras (datetime_represents=arrival) y gana el
    itinerario que deja salir mas tarde."""
    pedidas = _navitia(fake_prim, _salir_a_las_9(), _llegar_a_las_9())
    q = {"from": ADDR, "to": "stop_area:IDFM:65063", "when": "09:00"}
    sal = client.get("/api/plan", params={**q, "mode": "departure"}).json()["options"]
    lle = client.get("/api/plan", params={**q, "mode": "arrival"}).json()["options"]
    assert pedidas[0]["datetime_represents"] == ["departure"]
    assert pedidas[1]["datetime_represents"] == ["arrival"]
    assert pedidas[1]["datetime"][0].endswith("T090000")
    assert all(o["departure"] >= "09:00" for o in sal)
    assert sal[0]["minutes"] == min(o["minutes"] for o in sal)
    assert [o["departure"] for o in lle] == ["08:20", "08:12", "07:50"]
    assert lle[0]["departure"] == max(o["departure"] for o in lle)
    assert all(o["arrival"] <= "09:00" for o in lle)
    assert lle[0]["departure"] != sal[0]["departure"]
    # Un modo que no es ninguno de los dos se rechaza.
    r = client.get("/api/plan", params={**q, "mode": "cualquiera"})
    assert r.status_code == 400


def test_when_param_hora_pasada_es_manana():
    """R81: buscar a las 23:00 «para las 08:00» es mañana por la mañana."""
    noche = datetime(2026, 9, 24, 23, 0, tzinfo=settings.tz)
    assert planner.when_param("08:00", noche) == "20260925T080000"
    temprano = datetime(2026, 9, 24, 7, 0, tzinfo=settings.tz)
    assert planner.when_param("08:00", temprano) == "20260924T080000"
    assert planner.when_param("07:00", temprano) == "20260924T070000"
    # Fin de mes y de año.
    assert planner.when_param("06:30", datetime(2026, 12, 31, 22, 0, tzinfo=settings.tz)) \
        == "20270101T063000"
    for malo in (None, "", "8h", "25:00", "08:61", "ab:cd"):
        assert planner.when_param(malo, noche) is None


# =====================================================================
#  Traducir la direccion de Navitia a la del tiempo real: R32, R33
# =====================================================================

@pytest.fixture
async def pc(env, fake_prim):
    from app import db, prim
    db.init()
    await prim.startup()
    yield prim.get_client()
    await prim.shutdown()


async def test_resolve_direction(pc, fake_prim):
    """Igualdad normalizada, contencion o prefijo; sin coincidencia, lista
    vacia (se ven todos los pasos, y se avisa)."""
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 4),
                  Dep("C01739", "Mantes-la-Jolie", 9),
                  Dep("C01739", "Marne-la-Vallée - Chessy", 12),
                  Dep("C01383", "Châtillon Montrouge", 3))
    r = planner.resolve_direction
    assert await r("stop_area:IDFM:71370", J[0], "ERMONT – EAUBONNE") == ["Ermont - Eaubonne"]
    assert await r("stop_area:IDFM:71370", J[0], "Ermont - Eaubonne (Eaubonne)") == \
        ["Ermont - Eaubonne"]
    assert await r("stop_area:IDFM:71370", J[0], "Mantes-la-Jolie (Mantes-la-Jolie)") == \
        ["Mantes-la-Jolie"]
    assert await r("stop_area:IDFM:71370", J[0], "Marne-la-Vallée Chessy (Chessy)") == \
        ["Marne-la-Vallée - Chessy"]
    assert await r("stop_area:IDFM:71370", J[0], "Gisors") == []
    # Otra linea no presta sus destinos.
    assert await r("stop_area:IDFM:71370", J[0], "Châtillon Montrouge") == []
    llamadas = fake_prim.calls["stop-monitoring"]
    assert await r("stop_area:IDFM:71370", J[0], "") == []
    assert await r("", J[0], "Ermont") == []
    assert fake_prim.calls["stop-monitoring"] == llamadas          # sin gastar cuota
    fake_prim.fail_stations["65063"] = "timeout"
    assert await r("stop_area:IDFM:65063", J[0], "Ermont") == []    # PRIM caido


def test_from_plan_without_direction(client, fake_prim):
    """R33: los tramos que se quedan sin sentido (el texto de Navitia no casa
    con el del tiempo real) se dicen en `without_direction`."""
    S.fijar_reloj(fake_prim)
    fake_prim.add("71370", Dep("C01739", "Gare Saint-Lazare", 7))      # nada hacia Ermont
    fake_prim.add("71300", Dep("C01384", "Saint-Denis Pleyel", 3, aimed_delta=None))
    _navitia(fake_prim, _salir_a_las_9())
    opts = client.get("/api/plan", params={"from": ADDR, "to": "stop_area:IDFM:65063"}).json()
    dos_tramos = opts["options"][1]
    r = client.post("/api/routes/from-plan", json={"option": dos_tramos, "meta": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["without_direction"] == ["J"]
    assert [leg["directions"] for leg in body["route"]["legs"]] == [["Saint-Denis Pleyel"], []]
    assert body["route"]["name"] == "Madeleine → Argenteuil"
    assert body["route"]["days"] == [0, 1, 2, 3, 4]
    assert (body["route"]["time_from"], body["route"]["time_to"]) == ("07:00", "10:00")


# =====================================================================
#  Coordenadas de Navitia para el mapa
# =====================================================================

def test_route_from_plan_guarda_coordenadas(client, fake_prim):
    """Las coordenadas de subida y bajada de cada tramo salen del itinerario
    de Navitia y se guardan con la ruta (el mapa las usa de reserva), sin
    tocar la respuesta de /plan: PlanLeg es cerrado en el contrato."""
    from app import db
    _en_vivo(fake_prim)
    _navitia(fake_prim, _salir_a_las_9())
    plan = client.get("/api/plan", params={"from": ADDR, "to": "stop_area:IDFM:65063"}).json()
    schema_validator("Plan").validate(plan)
    assert not any("from_lat" in leg for o in plan["options"] for leg in o["legs"])

    r = client.post("/api/routes/from-plan", json={"option": plan["options"][1], "meta": {}})
    rid = r.json()["id"]
    assert not any("from_lat" in leg for leg in r.json()["route"]["legs"])
    legs = db.get_route(rid, with_coords=True)["legs"]
    # El 14 sube en Madeleine y baja en su anden de Saint-Lazare; la J sube
    # en SU anden de Saint-Lazare (otro punto de la misma zona).
    assert (legs[0]["from_lat"], legs[0]["from_lon"]) == (MADELEINE[2], MADELEINE[3])
    assert (legs[0]["to_lat"], legs[0]["to_lon"]) == (SL_14[2], SL_14[3])
    assert (legs[1]["from_lat"], legs[1]["from_lon"]) == (SL_J[2], SL_J[3])
    assert (legs[1]["to_lat"], legs[1]["to_lon"]) == (ARG[2], ARG[3])


def test_route_from_plan_sin_coordenadas_conocidas(client, fake_prim):
    """Si el servidor se reinicio entre planificar y guardar, la ruta se
    guarda igual, sin coordenadas; si el tramo ya las trae, mandan las suyas."""
    from app import db
    _en_vivo(fake_prim)
    _navitia(fake_prim, _salir_a_las_9())
    option = client.get("/api/plan", params={"from": ADDR, "to": "stop_area:IDFM:65063"}
                        ).json()["options"][0]
    planner._coords.clear()
    rid = client.post("/api/routes/from-plan", json={"option": option}).json()["id"]
    leg = db.get_route(rid, with_coords=True)["legs"][0]
    assert leg["from_lat"] is None and leg["to_lon"] is None

    option["legs"][0].update(from_lat=48.1, from_lon=2.1)
    rid = client.post("/api/routes/from-plan", json={"option": option}).json()["id"]
    leg = db.get_route(rid, with_coords=True)["legs"][0]
    assert (leg["from_lat"], leg["from_lon"], leg["to_lat"]) == (48.1, 2.1, None)


def test_coordenadas_de_la_respuesta_real_de_navitia():
    """Con el itinerario real guardado en tests/fixtures/prim: la coordenada
    es la del punto de parada (no la del centro de la zona)."""
    path = os.path.join(HERE, "fixtures", "prim", "navitia-journeys-saint-lazare-argenteuil.json")
    if not os.path.exists(path):
        pytest.skip("falta la respuesta real de Navitia (scripts/test-real.sh)")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    opts = planner.parse_journeys(data)
    assert opts and all(o["legs"] for o in opts)          # el non_pt_walk se descarta
    leg = opts[0]["legs"][0]
    c = planner.leg_coords(leg["line_id"], leg["from_id"], leg["to_id"])
    assert c == {"from_lat": 48.874669, "from_lon": 2.32517,
                 "to_lat": 48.948169, "to_lon": 2.255212}
    # De reserva, por parada sola (otra linea en la misma zona).
    assert planner.leg_coords("line:IDFM:C99999", leg["from_id"], "")["from_lat"] == 48.874669


# =====================================================================
#  De punta a punta (tools/test_plan.py y tools/test_arrival.py)
# =====================================================================

def _buscador(fake_prim):
    fake_prim.places["12 rue de rivoli"] = [
        {"embedded_type": "address", "name": "12 Rue de Rivoli (Paris)",
         "address": {"id": ADDR, "name": "12 Rue de Rivoli",
                     "administrative_regions": [{"name": "Paris"}]}},
        {"embedded_type": "stop_area", "name": "Hôtel de Ville (Paris)",
         "stop_area": {"id": "stop_area:IDFM:71222", "name": "Hôtel de Ville",
                       "administrative_regions": [{"name": "Paris"}]}},
        {"embedded_type": "poi", "name": "Tour Saint-Jacques (Paris)",
         "poi": {"id": "poi:osm:node:1", "name": "Tour Saint-Jacques"}}]
    fake_prim.places["saint-lazare"] = [
        {"embedded_type": "stop_area", "name": "Gare Saint-Lazare (Paris)",
         "stop_area": {"id": "stop_area:IDFM:71370", "name": "Gare Saint-Lazare",
                       "administrative_regions": [{"name": "Paris"}]}}]


def test_planificador_de_direccion_a_parada(client, fake_prim):
    """tools/test_plan.py: direccion y parada en el mismo buscador, planificar,
    guardar la opcion, ver su tablero y borrarla."""
    _buscador(fake_prim)
    _en_vivo(fake_prim)
    _navitia(fake_prim, _salir_a_las_9())

    p = client.get("/api/search/places", params={"q": "12 rue de Rivoli"}).json()["places"]
    assert p and any(x["kind"] == "dirección" for x in p)
    assert {x["kind"] for x in p} == {"dirección", "parada", "sitio"}
    addr = next(x for x in p if x["kind"] == "dirección")
    assert addr["id"] == ADDR and addr["city"] == "Paris"
    p2 = client.get("/api/search/places", params={"q": "Saint-Lazare"}).json()["places"]
    assert any(x["kind"] == "parada" for x in p2)

    plan = client.get("/api/plan", params={"from": addr["id"], "to": "stop_area:IDFM:71370",
                                           "when": "08:30"}).json()
    opts = plan["options"]
    assert len(opts) >= 1 and all(o["legs"] for o in opts)
    assert all(x["line_id"].startswith("line:IDFM:") and x["from_id"].startswith("stop_area:IDFM:")
               for o in opts for x in o["legs"])
    assert [o["minutes"] for o in opts] == sorted(o["minutes"] for o in opts)

    r = client.post("/api/routes/from-plan", json={
        "option": opts[0],
        "meta": {"name": "PRUEBA planificador", "origin_name": addr["name"],
                 "dest_name": "Gare Saint-Lazare", "days": [0, 1, 2, 3, 4],
                 "time_from": "08:00", "time_to": "09:30"}}).json()
    rid, ruta = r["id"], r["route"]
    assert rid and len(ruta["legs"]) == len(opts[0]["legs"])
    assert all(leg["line_id"] and leg["from_id"] for leg in ruta["legs"])
    assert (ruta["time_from"], ruta["time_to"]) == ("08:00", "09:30")
    assert ruta["legs"][0]["directions"] == ["Ermont - Eaubonne"] and r["without_direction"] == []

    b = client.get("/api/board", params={"route_id": rid, "log_history": "false"}).json()
    assert "legs" in b and any(leg["departures"] for leg in b["legs"])
    assert b["legs"][0]["departures"][0]["destination"] == "Ermont - Eaubonne"

    assert client.delete(f"/api/routes/{rid}").status_code == 200
    assert rid not in [x["id"] for x in client.get("/api/routes").json()["routes"]]


def test_guardar_como_llego_a_las_9(client, fake_prim):
    """tools/test_arrival.py: guardar «llego a las 09:00» con la duracion real
    del itinerario elegido; la franja acaba despues de la llegada y empieza
    con tiempo de sobra."""
    _en_vivo(fake_prim)
    _navitia(fake_prim, _salir_a_las_9(), _llegar_a_las_9())
    lle = client.get("/api/plan", params={"from": ADDR, "to": "stop_area:IDFM:65063",
                                          "when": "09:00", "mode": "arrival"}).json()["options"]
    r = client.post("/api/routes/from-plan", json={
        "option": lle[0],
        "meta": {"name": "PRUEBA llegada", "origin_name": "12 Rue de Rivoli",
                 "dest_name": "Argenteuil", "days": [0, 1, 2, 3, 4],
                 "time_mode": "arrival", "time_at": "09:00"}}).json()
    ruta = r["route"]
    assert ruta["time_mode"] == "arrival" and ruta["time_at"] == "09:00"
    assert ruta["duration_min"] == lle[0]["minutes"] == 32
    assert ruta["time_to"] >= "09:00" and ruta["time_from"] < ruta["time_at"]
    assert (ruta["time_from"], ruta["time_to"]) == ("07:43", "09:15")

    b = client.get("/api/board", params={"route_id": r["id"], "log_history": "false"}).json()
    assert b["legs"] and any(leg["departures"] for leg in b["legs"])
    assert client.delete(f"/api/routes/{r['id']}").status_code == 200


# =====================================================================
#  Alternativas: R79 (y R30)
# =====================================================================

def test_alternatives_usable_false(client, fake_prim, make_route):
    """R79: solo con una linea tocada o con force; se piden a Navitia
    excluyendo las lineas tocadas; una alternativa que pasa por otra linea
    interrumpida no es usable y va detras."""
    rid = make_route(ruta_j())
    por_el_14 = _viaje("09:00", "09:25", 25, 0,
                       _pt(M14, MADELEINE, SL_14, _t("09:02"), 23, "Saint-Denis Pleyel"))
    en_bus = _viaje("09:00", "09:45", 45, 0,
                    _pt(BUS_272, MADELEINE, PONT_BEZONS, _t("09:02"), 43, "Pont de Bezons"))
    pedidas = _navitia(fake_prim, {"journeys": [por_el_14, en_bus]})

    # Nada tocado: ni se pregunta a Navitia (comparte cuota con el buscador).
    r = client.get(f"/api/alternatives/{rid}").json()
    assert r == {"needed": False, "affected": [], "options": []}
    assert pedidas == []

    # Forzada sin nada tocado: se busca sin excluir nada.
    r = client.get(f"/api/alternatives/{rid}", params={"force": "true"}).json()
    assert r["needed"] is False and len(r["options"]) == 2
    assert all("forbidden_uris[]" not in q for q in pedidas)

    from app import prim
    fake_prim.message(Msg(["C01739"], "Trafic interrompu entre Saint-Lazare et Houilles."),
                      Msg(["C01384"], "Le trafic est interrompu sur toute la ligne."))
    prim.get_client().clear_cache()
    pedidas.clear()
    r = client.get(f"/api/alternatives/{rid}").json()
    assert r["needed"] is True
    assert r["affected"] == [{"line_id": "line:IDFM:C01739", "line_code": "J",
                              "level": 2, "label": "interrumpida"}]
    assert pedidas[0]["forbidden_uris[]"] == ["line:IDFM:C01739"]
    bus, metro = r["options"]
    assert bus["usable"] is True and bus["legs"][0]["code"] == "272"
    assert metro["usable"] is False and metro["worst_level"] == 2
    assert metro["legs"][0]["status"] == "interrumpida"
    assert r["baseline_minutes"] == 25 and bus["delta_minutes"] == 20
    assert client.get("/api/alternatives/999").status_code == 404
