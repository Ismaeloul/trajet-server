"""Mapa con datos abiertos de IDFM (app/mapdata.py, docs/datos-idfm.md §8).

Sin red: el portal y el zip GTFS los hace tests/_mapa_portal.py con las
muestras reales de tests/fixtures/idfm/. Las cifras esperadas son las medidas
de docs/datos-idfm.md (§3.1, §8.3, §8.4), con margen.
"""
from __future__ import annotations

import os
import tracemalloc

import pytest
import yaml
from _mapa_portal import GTFS_ETAG, FakePortal, fixture
from conftest import FAKE_KEY, ruta_j
from jsonschema import Draft202012Validator

HERE = os.path.dirname(os.path.abspath(__file__))

# Zona que no esta en las muestras: el portal falso no sabe nada de ella y el
# mapa tiene que apanarse con las coordenadas guardadas del tramo.
ZONA_FUERA = "stop_area:IDFM:0"
JEAN_MOULIN = (48.93869385820138, 2.242830150410148)    # IDFM:40068, real (ver _mapa_portal)

J_VIA = ["Asnières-sur-Seine", "Bois-Colombes", "Colombes", "Le Stade"]


# ---------------- montaje ----------------

@pytest.fixture
def portal_idfm(env, monkeypatch):
    """Mapa encendido, BD migrada y el portal falso enchufado (antes de
    arrancar la app, si el test la usa)."""
    from app import db, mapdata
    from app.config import settings
    monkeypatch.setenv("TRAJET_MAP", "1")
    settings.reload()
    db.init()
    portal = FakePortal()
    mapdata._reset()
    monkeypatch.setattr(mapdata, "transport_override", portal.transport)
    monkeypatch.setattr(mapdata, "RETRY_WAITS", (0.0, 0.0))
    yield portal
    mapdata._reset()


@pytest.fixture
async def mapa(portal_idfm):
    """Lo mismo para los tests que llaman al modulo directamente."""
    from app import mapdata
    yield portal_idfm
    await mapdata.shutdown()


def _spec() -> dict:
    with open(os.path.join(HERE, "contract", "openapi.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validator(name: str) -> Draft202012Validator:
    spec = _spec()
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": spec["components"]},
                                format_checker=Draft202012Validator.FORMAT_CHECKER)


def ruta_j_y_272() -> dict:
    """J de Saint-Lazare a Argenteuil y el bus 272 hasta Jean Moulin - Henri
    Barbusse (zona fuera de las muestras, con las coordenadas que guardaria
    el planificador)."""
    r = ruta_j(dest_id=ZONA_FUERA, dest_name="Jean Moulin - Henri Barbusse")
    r["legs"].append({
        "line_id": "line:IDFM:C01254", "line_code": "272", "line_name": "272",
        "line_mode": "Bus", "line_color": "FF5A00",
        "from_id": "stop_area:IDFM:65063", "from_name": "Argenteuil",
        "to_id": ZONA_FUERA, "to_name": "Jean Moulin - Henri Barbusse",
        "to_lat": JEAN_MOULIN[0], "to_lon": JEAN_MOULIN[1],
        "directions": [],
    })
    return r


async def _calcular(route_id: int) -> dict:
    """Encola el calculo, espera a que acabe y devuelve lo que se serviria."""
    from app import db, mapdata
    mapdata.schedule_route(route_id)
    await mapdata.wait_idle()
    return await mapdata.route_map(db.get_route(route_id, with_coords=True))


def _guardar(route: dict | None = None) -> int:
    from app import db
    return db.save_route(route or ruta_j())


def _estructuras_j():
    """Estaciones, paradas y trazado de la J ya leidos de las muestras."""
    from app import mapdata as M
    arr = {"features": fixture("arrets-saint-lazare.geojson")["features"]
           + fixture("arrets-argenteuil.geojson")["features"]}
    st = M._build_stations(fixture("zdc-saint-lazare-argenteuil.json"),
                           fixture("zda-saint-lazare-argenteuil.json"), arr)
    stops = M._parse_stops(fixture("arrets-lignes-J.json"))
    parts = M._parse_parts(fixture("traces-gtfs-ligne-J.geojson.gz"))
    leg = {"seq": 0, "code": "C01739", "zfrom": "71370", "zto": "65063", "mode": "rail",
           "from_name": "Gare Saint-Lazare", "to_name": "Argenteuil",
           "from_lat": None, "from_lon": None, "to_lat": None, "to_lon": None}
    return st, stops, parts, leg


def _ferre_j():
    from app import mapdata as M
    tramos = M._parse_ferre(fixture("traces-ferre-J-saint-lazare-argenteuil.geojson"), "C01739")
    gares = M._parse_gares(fixture("gares-saint-lazare-argenteuil.geojson"), "C01739", {"TRAIN J"})
    return {"tramos": tramos, "gares": gares}


# ---------------- geometria ----------------

def test_recorte_j_saint_lazare_argenteuil():
    """El recorte del trazado GTFS de la J entre Saint-Lazare y Argenteuil
    mide 9,7 km (medido: 9 769 m) y sale de las paradas de los trenes."""
    from app import mapdata as M
    st, stops, parts, leg = _estructuras_j()
    body = M._solve_leg(leg, stops, parts, st["71370"], st["65063"])
    assert body["source"] == "gtfs"
    assert abs(body["length_m"] - 9700) <= 200
    assert body["from"]["stop_id"] == "IDFM:monomodalStopPlace:58566"
    assert body["to"]["stop_id"] == "IDFM:monomodalStopPlace:47875"


def test_recorte_j_sentido_contrario():
    from app import mapdata as M
    st, stops, parts, leg = _estructuras_j()
    body = M._solve_leg(dict(leg, zfrom="65063", zto="71370"), stops, parts, st["65063"], st["71370"])
    assert body["source"] == "gtfs"
    assert abs(body["length_m"] - 9700) <= 200
    assert [v["name"] for v in body["via"]] == list(reversed(J_VIA))


def test_paradas_intermedias_j():
    from app import mapdata as M
    st, stops, parts, leg = _estructuras_j()
    body = M._solve_leg(leg, stops, parts, st["71370"], st["65063"])
    assert [v["name"] for v in body["via"]] == J_VIA


def test_plan_b_ferroviario_j():
    """Sin trazado GTFS: grafo de tramos ferroviarios y Dijkstra. El tramo 550
    (`idrefligc='C0173'`, truncado) entra en el grafo por su `res_com`."""
    from app import mapdata as M
    ferre = _ferre_j()
    assert 550 in [oid for oid, _ in ferre["tramos"]]
    fb = M._plan_ferre(ferre["tramos"], ferre["gares"]["71370"], ferre["gares"]["65063"])
    assert fb["tramos"] == [279, 321, 280, 283, 282, 281]
    assert abs(fb["length"] - 9830) <= 100

    st, stops, _, leg = _estructuras_j()
    assert M._solve_leg(leg, stops, [], st["71370"], st["65063"]) == {"need_ferre": True}
    body = M._solve_leg(leg, stops, [], st["71370"], st["65063"], ferre)
    assert body["source"] == "ferre"
    assert body["tramos"] == [279, 321, 280, 283, 282, 281]
    assert [v["name"] for v in body["via"]] == J_VIA


def test_plan_b_estacion_a_mitad_de_tramo():
    """Una estacion que cae en medio de un tramo parte el tramo en dos."""
    from app import mapdata as M
    ferre = _ferre_j()
    t279 = next(c for oid, c in ferre["tramos"] if oid == 279)
    medio = t279[len(t279) // 2]
    fb = M._plan_ferre(ferre["tramos"], medio, ferre["gares"]["65063"])
    assert fb["tramos"][0] == 279 and fb["tramos"][-1] == 281
    assert 5000 < fb["length"] < 9830 - 1000


def test_variante_bus_272():
    """El 272 tiene dos variantes entre Gare d'Argenteuil y Jean Moulin -
    Henri Barbusse (~2,0 y ~2,75 km, solapadas en el MultiLineString): se
    queda la corta, y de los dos postes el mas pegado al trazado."""
    from _mapa_portal import BUS_272_STOPS

    from app import mapdata as M
    parts = M._parse_parts(fixture("traces-gtfs-bus-272.geojson.gz"))
    stops = M._parse_stops(BUS_272_STOPS)
    cf = [s for s in stops if s["name"] == "Gare d'Argenteuil"]
    ct = [s for s in stops if s["name"].startswith("Jean Moulin")]
    largos = []
    for part in parts:
        xys = [M._xy(*p) for p in part]
        cum = M._cumulative(xys)
        for (_, a) in M._near_runs(M._xy(cf[0]["lon"], cf[0]["lat"]), xys, cum, 50):
            for (_, b) in M._near_runs(M._xy(ct[0]["lon"], ct[0]["lat"]), xys, cum, 50):
                if b > a:
                    largos.append(b - a)
    assert min(largos) < 2300 and max(largos) > 2600          # hay dos variantes
    plan = M._plan_gtfs(parts, cf, ct)
    assert 1900 < M.path_length(plan["coords"]) < 2300
    assert plan["from"]["id"] == "IDFM:40000"                 # 8 m del trazado (el otro, 13 m)


def test_douglas_peucker_puntos():
    """DP en metros: recorte GTFS 52 puntos a 2 m y 12 a 20 m; cadena
    ferroviaria 46 y 12 (docs/datos-idfm.md §8.4)."""
    from app import mapdata as M
    st, stops, parts, leg = _estructuras_j()
    gtfs = M._solve_leg(leg, stops, parts, st["71370"], st["65063"])
    ferre = M._solve_leg(leg, stops, [], st["71370"], st["65063"], _ferre_j())
    for body, fine in ((gtfs, 52), (ferre, 46)):
        assert abs(len(M.decode_polyline(body["path"]["fine"])) - fine) <= 8
        assert abs(len(M.decode_polyline(body["path"]["coarse"])) - 12) <= 3
        assert body["path"]["tolerance_m"] == {"coarse": 20, "fine": 2}
    # Lo que DP quita esta a menos de la tolerancia (el error no se nota).
    coords = M._parse_parts(fixture("traces-gtfs-ligne-J.geojson.gz"))[0]
    kept = M.douglas_peucker(coords, 20)
    xys = [M._xy(*p) for p in kept]
    cum = M._cumulative(xys)
    assert max(M._nearest(M._xy(*p), xys, cum)[0] for p in coords) <= 20.001


def test_polilinea_ida_y_vuelta():
    from app import mapdata as M
    # El ejemplo de la documentacion de Google.
    pts = [(-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)]
    assert M.encode_polyline(pts) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
    assert M.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    # Y un recorte real, con precision 5 (~1,1 m).
    coords = M.douglas_peucker(M._parse_parts(fixture("traces-gtfs-ligne-J.geojson.gz"))[0], 2)
    back = M.decode_polyline(M.encode_polyline(coords))
    assert len(back) == len(coords)
    for (lon, lat), (lat2, lon2) in zip(coords, back):
        assert abs(lat - lat2) <= 0.5e-5 + 1e-12 and abs(lon - lon2) <= 0.5e-5 + 1e-12


def test_lambert93_centroide_de_la_zona():
    """El centroide de la ZdC (Lambert 93) cae a menos de 5 m del punto de la
    misma zona en el stops.txt del GTFS (IDFM:71370)."""
    from app import mapdata as M
    lon, lat = M.lambert93_to_wgs84(650517, 6864202)
    assert M._dist((lon, lat), (2.325351594334675, 48.875942631522264)) < 5


# ---------------- el mapa de una ruta ----------------

async def test_route_map_valida_contrato(mapa):
    """El RouteMap cumple el esquema del contrato: pendiente, calculado y viejo."""
    from app import db, mapdata
    v = _validator("RouteMap")
    rid = _guardar()
    pendiente = await mapdata.route_map(db.get_route(rid, with_coords=True))
    v.validate(pendiente)
    body = await _calcular(rid)
    v.validate(body)
    assert body["pending"] is False and body["stale"] is False
    [line] = body["lines"]
    assert line["source"] == "gtfs" and line["mode"] == "rail" and line["code"] == "J"
    assert line["color"] == "#CEC73D" and line["text_color"] == "#000000"
    assert abs(line["length_m"] - 9700) <= 200
    assert [v["name"] for v in line["via"]] == J_VIA
    assert [(s["zdc"], s["role"]) for s in body["stations"]] == [("71370", "origin"), ("65063", "destination")]
    assert body["transfers"] == []
    # La fecha de la licencia es la del ultimo dato «Licence Ouverte» usado.
    assert "mise à jour du 23/09/2026" in body["license"]
    assert "© contributeurs OpenStreetMap" in body["license"]
    assert body["sources"]["traces-des-lignes-de-transport-en-commun-idfm"].startswith("2026-09-23T15:00:42")
    assert body["sources"]["offre-horaires-tc-gtfs-idfm"] == "2026-09-23T14:23:37+00:00"
    mapa.down = True
    await mapdata.refresh_all()                  # falla el catalogo: nada cambia
    v.validate(await mapdata.route_map(db.get_route(rid, with_coords=True)))


async def test_accesos_cour_du_havre_no_es_entrada(mapa):
    """Accesos de las ZdA que usa la ruta, con metros y segundos de
    pathways.txt; «cour du Havre» (50148490) es solo salida."""
    body = await _calcular(_guardar())
    acc = {a["id"]: a for a in body["accesses"]}
    havre = acc["50148490"]
    assert havre["entry"] is False and havre["exit"] is True and havre["number"] == "9"
    # Solo hay camino de la parada al acceso (salida): se da ese.
    assert havre["to_stop"] == [{"stop_id": "IDFM:monomodalStopPlace:58566", "m": 250.5, "s": 265}]
    londres = acc["50243328"]
    assert londres["entry"] and londres["number"] is None and londres["zdc"] == "71370"
    assert londres["to_stop"] == [{"stop_id": "IDFM:monomodalStopPlace:58566", "m": 149.46, "s": 158}]
    budapest = acc["50170773"]
    assert budapest["to_stop"][0]["m"] == 212.91 and budapest["to_stop"][0]["s"] == 226
    # Argenteuil: los 4 accesos de la ZdA de los trenes.
    assert {a["id"] for a in body["accesses"] if a["zdc"] == "65063"} == {
        "50170428", "50170422", "50170309", "50170427"}
    # Los accesos del metro que no sirven a la ZdA de los trenes no salen.
    assert "50148706" not in acc


async def test_vias_de_saint_lazare(mapa):
    body = await _calcular(_guardar())
    sl = next(s for s in body["stations"] if s["zdc"] == "71370")
    voies = [t["voie"] for t in sl["tracks"]]
    assert voies == [str(i) for i in range(1, 28)]            # 6 y 7 comparten arrid
    assert sl["platforms"] == []                               # la J para en la ZdA, sin andenes Q:
    arg = next(s for s in body["stations"] if s["zdc"] == "65063")
    assert arg["tracks"] == []                                 # no hay muestra de sus vias


async def test_transbordo_y_bus_con_coordenadas(mapa):
    """J + 272: transbordo en Argenteuil con el tiempo de transfers.txt y el
    bus recortado hasta las coordenadas guardadas del tramo."""
    body = await _calcular(_guardar(ruta_j_y_272()))
    _validator("RouteMap").validate(body)
    j, bus = body["lines"]
    assert j["source"] == "gtfs" and bus["source"] == "gtfs"
    assert bus["mode"] == "bus" and bus["color"] == "#FF5A00" and bus["text_color"] == "#000000"
    assert 1900 < bus["length_m"] < 2300
    assert bus["from"]["stop_id"] in ("IDFM:40000", "IDFM:39839")
    assert bus["to"]["stop_id"] is None and bus["to"]["zdc"] == "0"
    [t] = body["transfers"]
    esperado = {"IDFM:40000": 394, "IDFM:39839": 389}[bus["from"]["stop_id"]]
    assert t == {"zdc": "65063", "from_seq": 0, "to_seq": 1, "min_transfer_s": esperado}
    roles = [(s["zdc"], s["role"]) for s in body["stations"]]
    assert roles == [("71370", "origin"), ("65063", "transfer"), ("0", "destination")]
    arg = body["stations"][1]
    assert {p["id"] for p in arg["platforms"]} == {"STIF:StopPoint:Q:40000:", "STIF:StopPoint:Q:39839:"}
    assert all(p["line"] == "C01254" for p in arg["platforms"])


async def test_ruta_nueva_pending(mapa):
    """Ruta nueva: se responde enseguida con `pending` y lo que haya, sin
    esperar a la red; el calculo va en segundo plano."""
    from app import db, mapdata
    rid = _guardar()
    # Con el calculo bloqueado: route_map responde igual, sin esperarlo.
    async with mapdata._semaphore():
        body = await mapdata.route_map(db.get_route(rid, with_coords=True))
        assert mapa.requests == []                           # route_map no toca la red
    assert body["pending"] is True and body["lines"] == []   # sin coordenadas no hay que pintar
    await mapdata.wait_idle()
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["pending"] is False and body["lines"][0]["source"] == "gtfs"

    # Otra ruta con el mismo tramo: sale ya pintada de lo guardado, aunque
    # pendiente de su propio calculo.
    otra = _guardar(ruta_j(name="otra"))
    body = await mapdata.route_map(db.get_route(otra, with_coords=True))
    assert body["pending"] is True and body["lines"][0]["source"] == "gtfs"
    assert body["stations"] and body["accesses"]
    await mapdata.wait_idle()


async def test_ruta_editada_vuelve_a_pending(mapa):
    from app import db, mapdata
    rid = _guardar()
    await _calcular(rid)
    db.save_route(ruta_j_y_272(), rid)
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["pending"] is True
    await mapdata.wait_idle()
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["pending"] is False and len(body["lines"]) == 2


async def test_portal_caido_sirve_lo_guardado_stale(mapa):
    """Si el portal cae en el refresco se sirve lo guardado con stale=true y
    no se borra nada bueno; al volver, stale=false."""
    from app import db, mapdata
    rid = _guardar()
    bueno = await _calcular(rid)
    # Todo ha cambiado en el portal... pero el portal se cae al ir a por ello.
    for ds in mapdata.CATALOG_DATASETS:
        mapa.set_processed(ds, "2026-10-30T00:00:00+00:00")
    mapa.status = {ds: 503 for ds in mapdata.CATALOG_DATASETS}
    await mapdata.refresh_all()
    viejo = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert viejo["stale"] is True and viejo["pending"] is False
    assert viejo["lines"] == bueno["lines"] and viejo["accesses"] == bueno["accesses"]
    st = mapdata.status()
    assert st["last_error"] and "503" in st["last_error"]
    # Vuelve el portal (y han pasado los 30 min de pausa).
    mapa.status = {}
    mapdata._portal._paused_until.clear()
    await mapdata.refresh_all()
    nuevo = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert nuevo["stale"] is False and nuevo["lines"][0]["path"] == bueno["lines"][0]["path"]
    assert mapdata.status()["last_error"] is None


async def test_portal_caido_ruta_nueva_recta(mapa):
    """Portal caido y ruta nueva: la recta entre las coordenadas guardadas
    (si las hay), marcada como vieja; nada de eso se guarda como bueno."""
    from app import db, mapdata
    mapa.down = True
    r = ruta_j()
    r["legs"][0].update({"from_lat": 48.877476, "from_lon": 2.324439,
                         "to_lat": 48.946895, "to_lon": 2.257914})
    rid = _guardar(r)
    body = await _calcular(rid)
    _validator("RouteMap").validate(body)
    assert body["stale"] is True and body["pending"] is False
    [line] = body["lines"]
    assert line["source"] == "recta" and line["path"] is not None and line["via"] == []
    assert 8000 < line["length_m"] < 9500
    rows = mapdata._db_get(["leg:C01739:71370:65063"])
    assert rows == {}                                 # la recta no pisa nunca un tramo bueno
    # Vuelve el portal: el reintento lo arregla.
    mapa.down = False
    mapdata._portal._paused_until.clear()
    await mapdata.refresh_all(full=False)
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["stale"] is False and body["lines"][0]["source"] == "gtfs"


async def test_sin_trazado_gtfs_usa_el_ferrocarril(mapa):
    mapa.override["traces-des-lignes-de-transport-en-commun-idfm"] = {"type": "FeatureCollection", "features": []}
    body = await _calcular(_guardar())
    [line] = body["lines"]
    assert line["source"] == "ferre" and abs(line["length_m"] - 9830) <= 100
    assert [v["name"] for v in line["via"]] == J_VIA
    assert "traces-du-reseau-ferre-idf" in body["sources"]
    [req] = mapa.exports("traces-du-reseau-ferre-idf")
    assert "idrefligc='C01739'" in req.url.params["where"] and "indice_lig='J'" in req.url.params["where"]


async def test_colores_de_la_ruta_sin_referencial(mapa):
    mapa.override["referentiel-des-lignes"] = []
    body = await _calcular(_guardar())
    line = body["lines"][0]
    # El color que trajo Navitia al guardar la ruta, y un texto que se lea.
    assert line["color"] == "#CEC73D" and line["text_color"] == "#000000" and line["mode"] == "rail"


# ---------------- red, cache y recursos ----------------

async def test_mapdata_sin_apikey(mapa):
    """R90: ninguna peticion al portal ni al GTFS lleva la cabecera `apikey`
    (ni la clave en ninguna parte), aunque haya clave de PRIM configurada."""
    from app import mapdata
    from app.config import settings
    assert settings.api_key == FAKE_KEY
    await _calcular(_guardar(ruta_j_y_272()))
    await mapdata.refresh_all()
    assert mapa.portal_requests() and mapa.gtfs_requests()
    for r in mapa.requests:
        assert "apikey" not in {k.lower() for k in r.headers.keys()}
        assert FAKE_KEY not in str(r.url)
        assert all(FAKE_KEY not in v for v in r.headers.values())
        assert r.headers["user-agent"].startswith("Trajet/")
    # Ni aunque alguien la meta a mano.
    await mapdata._portal.request("https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets",
                                  headers={"apikey": FAKE_KEY})
    assert "apikey" not in mapa.requests[-1].headers


async def test_sin_volcados_completos(mapa):
    """Toda exportacion lleva `where` (y `select`); el zip GTFS nunca se baja
    entero: solo trozos con Range."""
    from app import mapdata
    await _calcular(_guardar(ruta_j_y_272()))
    exports = mapa.exports()
    assert exports
    for r in exports:
        assert r.url.params.get("where") and r.url.params.get("select")
    with pytest.raises(ValueError):
        await mapdata._portal.export("arrets", "json", "")
    gtfs = mapa.gtfs_requests()
    assert gtfs and all(r.headers.get("Range") for r in gtfs)


async def test_gtfs_por_range(mapa):
    """pathways.txt y transfers.txt se leen del zip por Range y filtrados."""
    from app import mapdata
    z = await mapdata._gtfs_open()
    assert z.etag == GTFS_ETAG and set(z.members) == {"pathways.txt", "transfers.txt"}
    got = {}

    def keep(r):
        got[(r["from_stop_id"], r["to_stop_id"])] = r["min_transfer_time"]
    await mapdata._gtfs_read(z, "transfers.txt", mapdata._CsvFilter(
        z.members["transfers.txt"][0], ("IDFM:monomodalStopPlace:58566",), keep))
    assert got[("IDFM:monomodalStopPlace:58566", "IDFM:462972")] == "360"
    # Final del zip, cabecera local y el miembro: una parte pequena del zip.
    assert mapdata._portal.requests == 3
    assert mapa.gtfs_bytes < len(mapa.gtfs_zip) / 2
    assert await mapdata._gtfs_open(if_none_match=GTFS_ETAG) is None      # 304


async def test_gtfs_sin_range_no_se_baja_el_zip(mapa):
    """Si el servidor del GTFS ignora el Range, se corta sin leer el cuerpo y
    los accesos salen con metros y segundos nulos (el contrato lo permite)."""
    mapa.gtfs_ignores_range = True
    body = await _calcular(_guardar())
    assert body["stale"] is True
    londres = next(a for a in body["accesses"] if a["id"] == "50243328")
    assert londres["to_stop"] == [{"stop_id": "IDFM:monomodalStopPlace:58566", "m": None, "s": None}]
    _validator("RouteMap").validate(body)


async def test_reintentos_y_pausa(mapa, monkeypatch):
    """Dos reintentos; tras tres fallos seguidos, 30 min sin intentarlo."""
    from app import mapdata
    mapa.down = True
    with pytest.raises(mapdata.PortalError):
        await mapdata._portal.catalog(mapdata.CATALOG_DATASETS)
    assert len(mapa.requests) == 3
    with pytest.raises(mapdata.PortalError, match="pausa"):
        await mapdata._portal.catalog(mapdata.CATALOG_DATASETS)
    assert len(mapa.requests) == 3                      # ni lo ha intentado
    # Un 4xx no es «portal caido»: ni se reintenta ni abre la pausa.
    mapdata._reset()
    monkeypatch.setattr(mapdata, "transport_override", mapa.transport)
    mapa.down = False
    mapa.status = {"arrets": 400}
    with pytest.raises(mapdata.PortalError) as e:
        await mapdata._portal.export("arrets", "json", "zdaid in ('1')")
    assert e.value.kind == "http" and len(mapa.exports("arrets")) == 1
    assert not mapdata._portal.paused("data.iledefrance-mobilites.fr")


async def test_frescura_con_el_catalogo(mapa):
    """El refresco diario hace UNA llamada al catalogo y solo vuelve a pedir
    lo que tiene `data_processed` nuevo."""
    from app import db, mapdata
    rid = _guardar()
    await _calcular(rid)
    antes = len(mapa.requests)
    await mapdata.refresh_all()
    nuevas = mapa.requests[antes:]
    assert len(nuevas) == 1 and "/exports/" not in nuevas[0].url.path     # solo el catalogo
    # Cambian los accesos: se piden de nuevo (solo relations-acces y acces).
    mapa.set_processed("acces", "2026-09-24T00:30:00+00:00")
    antes = len(mapa.requests)
    await mapdata.refresh_all()
    pedidos = {r.url.path.split("/datasets/")[1].split("/")[0] for r in mapa.requests[antes:]
               if "/exports/" in r.url.path}
    assert pedidos == {"relations-acces", "acces"}
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["sources"]["acces"] == "2026-09-24T00:30:00+00:00"


async def test_memoria_pico(mapa):
    """Presupuesto del modulo: <= 20 MB de pico calculando una ruta (§8.8)."""
    from app import mapdata
    rid = _guardar(ruta_j_y_272())
    tracemalloc.start()
    try:
        mapdata.schedule_route(rid)
        await mapdata.wait_idle()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 20 * 1024 * 1024, f"pico de {peak / 1e6:.1f} MB"


async def test_un_calculo_a_la_vez(mapa, monkeypatch):
    """asyncio.Semaphore(1): nunca dos calculos a la vez (memoria, §8.8)."""
    from app import mapdata
    activos = maximo = 0
    real = mapdata._build

    async def build(*a, **k):
        nonlocal activos, maximo
        activos += 1
        maximo = max(maximo, activos)
        try:
            return await real(*a, **k)
        finally:
            activos -= 1
    monkeypatch.setattr(mapdata, "_build", build)
    for r in (ruta_j(), ruta_j_y_272(), ruta_j(name="x")):
        mapdata.schedule_route(_guardar(r))
    await mapdata.wait_idle()
    assert maximo == 1


async def test_status_para_el_panel(mapa):
    from app import mapdata
    st = mapdata.status()
    assert st == {"cached_items": 0, "last_refresh": None, "last_error": None}
    await _calcular(_guardar())
    st = mapdata.status()
    assert st["cached_items"] > 5 and st["last_refresh"] and st["last_error"] is None
    spec = _spec()["components"]["schemas"]["AdminOverview"]["properties"]["map_data"]
    Draft202012Validator(spec).validate(st)


async def test_mapa_apagado(env):
    """TRAJET_MAP=0: ni tarea de fondo, ni cola, ni red."""
    from app import db, mapdata
    mapdata._reset()
    db.init()
    await mapdata.startup()
    assert mapdata._refresher_task is None
    rid = _guardar()
    mapdata.schedule_route(rid)
    assert mapdata._queued == {}
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["pending"] is False and body["lines"] == []
    await mapdata.shutdown()


async def test_arranque_y_parada(mapa, monkeypatch):
    from app import mapdata
    monkeypatch.setattr(mapdata, "STARTUP_DELAY", 3600)
    await mapdata.startup()
    assert mapdata._refresher_task is not None and not mapdata._refresher_task.done()
    await mapdata.shutdown()
    assert mapdata._refresher_task is None


async def test_encolar_desde_un_hilo(mapa, monkeypatch):
    """La API guarda las rutas en un hilo (run_in_threadpool): encolar desde
    ahi tambien funciona."""
    import asyncio

    from fastapi.concurrency import run_in_threadpool

    from app import db, mapdata
    monkeypatch.setattr(mapdata, "STARTUP_DELAY", 3600)
    await mapdata.startup()
    rid = _guardar()
    await run_in_threadpool(mapdata.schedule_route, rid)
    for _ in range(100):
        if mapdata._queued:
            break
        await asyncio.sleep(0.01)
    await mapdata.wait_idle()
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["pending"] is False and body["lines"][0]["source"] == "gtfs"


def test_mapa_por_la_api_v1(portal_idfm, client):
    """De punta a punta por /api/v1 con el mapa encendido: primero `pending`,
    luego el mapa calculado en segundo plano y despues 304 con el ETag."""
    import time

    from _seg_contrato import ContractClient, pair

    v1 = ContractClient(client)
    api = v1.with_token(pair(v1)["token"])
    rid = api.post("/api/v1/routes", json=ruta_j()).json()["id"]
    body = api.get(f"/api/v1/routes/{rid}/map").json()
    limite = time.monotonic() + 10
    while body["pending"] and time.monotonic() < limite:
        time.sleep(0.05)
        body = api.get(f"/api/v1/routes/{rid}/map").json()
    assert body["pending"] is False and body["stale"] is False
    assert body["lines"][0]["source"] == "gtfs" and body["accesses"]
    r = api.get(f"/api/v1/routes/{rid}/map")
    r2 = api.get(f"/api/v1/routes/{rid}/map", headers={"If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304
    assert all("apikey" not in req.headers for req in portal_idfm.requests)


async def test_mapa_viejo_se_mejora_sin_empeorar(mapa):
    """Primero todo caido (recta); luego vuelve el portal pero no el GTFS:
    el mapa mejora (trazado de verdad) aunque siga `stale`; y un fallo
    posterior no lo cambia por algo peor."""
    from app import db, mapdata
    r = ruta_j()
    r["legs"][0].update({"from_lat": 48.877476, "from_lon": 2.324439,
                         "to_lat": 48.946895, "to_lon": 2.257914})
    rid = _guardar(r)
    mapa.down = mapa.gtfs_down = True
    assert (await _calcular(rid))["lines"][0]["source"] == "recta"
    mapa.down = False
    mapdata._portal._paused_until.clear()
    await mapdata.refresh_all(full=False)
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["stale"] is True and body["lines"][0]["source"] == "gtfs"
    assert all(t["m"] is None for a in body["accesses"] for t in a["to_stop"])
    # Ahora el portal responde pero sus datos fallan, y el tramo guardado ya
    # no esta: lo nuevo seria una recta, peor que lo que se sirve.
    mapa.status = {ds: 503 for ds in mapdata.CATALOG_DATASETS}
    with db.conn() as c:
        c.execute("DELETE FROM map_cache WHERE key LIKE 'leg:%'")
    await mapdata.refresh_all(full=False)
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["stale"] is True and body["lines"][0]["source"] == "gtfs"
    # Vuelve todo: mapa completo y al dia.
    mapa.status = {}
    mapa.gtfs_down = False
    mapdata._portal._paused_until.clear()
    await mapdata.refresh_all(full=False)
    body = await mapdata.route_map(db.get_route(rid, with_coords=True))
    assert body["stale"] is False
    assert any(t["m"] is not None for a in body["accesses"] for t in a["to_stop"])
