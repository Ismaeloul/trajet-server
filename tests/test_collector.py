"""Recolector de andenes (app/collector.py): R77 y R78.

Incluye lo que comprobaba tools/test_platform.py sobre el recolector (que
estaciones estudia, la franja solo decide el ritmo, el ritmo se adapta a la
cuota, el tope de estaciones y el reparto hasta la medianoche UTC), y los
arreglos de docs/servidor.md: 18.3.11 (no toca la memoria de via nueva del
tablero) y 18.4 (SQLite fuera del bucle de eventos, sin get_route de mas).
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from conftest import ruta_j
from fakeprim import Dep

from app import collector as C
from app.config import settings

UTC = timezone.utc
STOP = "stop_area:IDFM:71370"
LINE = "line:IDFM:C01739"

RUTA = {"days": [0, 1, 2, 3, 4], "time_from": "07:00", "time_to": "10:00",
        "legs": [
            {"from_id": STOP, "line_id": LINE, "line_mode": "Train Transilien"},
            {"from_id": "stop_area:IDFM:71264", "line_id": "line:IDFM:C01383",
             "line_mode": "Métro"},
        ]}


def _lunes(hora: int, minuto: int = 0) -> datetime:
    """El proximo lunes a esa hora de Paris."""
    d = datetime.now(settings.tz).replace(hour=hora, minute=minuto, second=0, microsecond=0)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


@pytest.fixture
async def pc(env, fake_prim):
    from app import db, prim
    db.init()
    await prim.startup()
    yield prim.get_client()
    await prim.shutdown()


def _refs(fake_prim) -> list[str]:
    """MonitoringRef de cada stop-monitoring que llega al PRIM falso."""
    pedidas: list[str] = []
    original = fake_prim._stop_monitoring

    def apuntar(ref):
        pedidas.append(ref)
        return original(ref)

    fake_prim._stop_monitoring = apuntar
    return pedidas


# =====================================================================
#  R77: que se estudia
# =====================================================================

def test_collector_objetivos(env, make_route):
    """R77: solo las estaciones de las rutas guardadas, sin los modos que no
    publican via, como mucho MAX_ESTACIONES y las de las rutas que USO
    primero."""
    from app import db
    t = C.targets([RUTA])
    assert list(t) == [STOP]                          # el metro se descarta
    assert t[STOP][0]["line_id"] == LINE
    sin_via = {"days": [0], "legs": [
        {"from_id": f"stop_area:IDFM:{i}", "line_id": "line:IDFM:C0", "line_mode": modo}
        for i, modo in enumerate(("Métro", "METRO", "Bus", "Tram", "Tramway",
                                  "Funiculaire", "Funicular"))]}
    assert C.targets([sin_via]) == {}
    assert C.targets([{"legs": [{"from_id": "", "line_id": LINE, "line_mode": "RER"}]}]) == {}
    # Dos tramos de la misma estacion: una sola entrada (una llamada).
    doble = {"legs": [{"from_id": STOP, "line_id": LINE, "line_mode": "Train"},
                      {"from_id": STOP, "line_id": "line:IDFM:C01743", "line_mode": "RER"}]}
    assert [len(v) for v in C.targets([doble]).values()] == [2]

    # Nueve rutas con nueve estaciones: como mucho MAX_ESTACIONES, y primero
    # las que mas se han consultado en los ultimos 14 dias.
    ids = [make_route(ruta_j(name=f"r{i}", legs=[dict(ruta_j()["legs"][0],
                                                     from_id=f"stop_area:IDFM:{7000 + i}")]))
           for i in range(9)]
    hoy = datetime.now(settings.tz).strftime("%Y-%m-%d")
    for rid, veces in ((ids[8], 3), (ids[6], 2)):
        for _ in range(veces):
            db.log_observation(rid, datetime.now(UTC).isoformat(), hoy, 0, 0, "", {})
    t = C.targets(db.list_routes())
    assert len(t) == C.MAX_ESTACIONES
    assert list(t)[:2] == ["stop_area:IDFM:7008", "stop_area:IDFM:7006"]


async def test_collector_solo_pide_las_estaciones_de_mis_rutas(pc, fake_prim, make_route):
    """R77 de punta a punta: la pasada solo pide la estacion del tren de la
    ruta guardada; la del metro no se sondea."""
    ruta = ruta_j(legs=[ruta_j()["legs"][0],
                        {**ruta_j()["legs"][0], "line_id": "line:IDFM:C01383",
                         "line_code": "13", "line_mode": "Métro",
                         "from_id": "stop_area:IDFM:490003"}])
    make_route(ruta)
    pedidas = _refs(fake_prim)
    res = await C.sample_once(now=_lunes(14))
    assert res["stations"] == 1 and pedidas == ["STIF:StopArea:SP:71370:"]


# =====================================================================
#  R78: cada cuanto
# =====================================================================

def test_plan_interval():
    """R78: nunca toca las RESERVA llamadas de la pantalla, no muestrea de
    01:00 a 05:00 y va entre MIN_INTERVAL y MAX_INTERVAL."""
    tarde = _lunes(14)
    assert C.RESERVA == 320 and (C.MIN_INTERVAL, C.MAX_INTERVAL) == (120, 1800)
    # La reserva no se toca: con RESERVA justas, nada; con una mas, algo.
    i, motivo = C.plan_interval(C.RESERVA, 1, tarde, priority=False)
    assert i == 0 and "cuota agotada" in motivo and "reservado para la pantalla" in motivo
    # Con una sola llamada util: el maximo (y un tercio en franja).
    assert C.plan_interval(C.RESERVA + 1, 1, tarde, priority=False)[0] == C.MAX_INTERVAL
    assert C.plan_interval(C.RESERVA + 1, 1, tarde, priority=True)[0] ==         C.MAX_INTERVAL / C.PRIORIDAD
    assert C.plan_interval(0, 1, tarde, priority=False)[0] == 0
    # Horas muertas: de 01:00 a 04:59 nada; a las 00:59 y a las 05:00 si.
    for h, m, calla in ((0, 59, False), (1, 0, True), (3, 0, True), (4, 59, True),
                        (5, 0, False)):
        i, motivo = C.plan_interval(C.RESERVA + 600, 1, _lunes(h, m), priority=False)
        assert (i == 0) is calla, (h, m, motivo)
        if calla:
            assert "horas muertas" in motivo
    # Siempre entre el minimo y el maximo, con cualquier cuota y estaciones.
    for restante in (C.RESERVA + 1, C.RESERVA + 50, 700, 1000, 10_000):
        for estaciones in (1, 2, 4):
            for prioridad in (False, True):
                i, _ = C.plan_interval(restante, estaciones, tarde, prioridad)
                assert C.MIN_INTERVAL <= i <= C.MAX_INTERVAL, (restante, estaciones, i)


def test_ritmo_se_adapta_a_la_cuota():
    """Lo de tools/test_platform.py: lazo cerrado con la cuota que queda."""
    tarde = _lunes(14)
    mucho = C.RESERVA + 600
    poco = C.RESERVA + 60
    i_mucho, _ = C.plan_interval(mucho, 1, tarde, priority=False)
    i_poco, _ = C.plan_interval(poco, 1, tarde, priority=False)
    assert i_mucho <= i_poco                       # con mucha cuota, mas a menudo
    assert i_mucho >= C.MIN_INTERVAL and i_poco <= C.MAX_INTERVAL

    # Con presupuesto justo la prioridad decide: en franja, 3 veces mas.
    manana = _lunes(8)
    i_pri, motivo = C.plan_interval(poco, 1, manana, priority=True)
    i_no, _ = C.plan_interval(poco, 1, manana, priority=False)
    assert i_pri < i_no and "en franja de ruta" in motivo
    # Con cuota de sobra los dos van al minimo.
    assert C.plan_interval(mucho, 1, manana, priority=True)[0] == C.MIN_INTERVAL

    # Dos estaciones cuestan el doble por pasada: espacia mas.
    assert C.plan_interval(mucho, 2, tarde, False)[0] >= C.plan_interval(mucho, 1, tarde, False)[0]
    # Sin rutas de tren, nada; sin saber la cuota, prudente (y la aprende).
    i, motivo = C.plan_interval(mucho, 0, tarde, priority=False)
    assert i == 0 and "ninguna ruta" in motivo
    i, motivo = C.plan_interval(None, 1, tarde, priority=False)
    assert i == 300 and "desconocida" in motivo


def test_formula_hasta_la_medianoche_utc():
    """intervalo = segundos hasta el reinicio x estaciones / llamadas utiles."""
    assert abs(C.seconds_to_reset(datetime(2026, 8, 31, 18, 0, tzinfo=UTC)) - 6 * 3600) < 2
    assert abs(C.seconds_to_reset(datetime(2026, 8, 31, 23, 30, tzinfo=UTC)) - 1800) < 2
    assert C.seconds_to_reset(datetime(2026, 8, 31, 23, 59, 59, 999999, tzinfo=UTC)) >= 1.0
    # 18:00 UTC = 20:00 de Paris en verano: 6 h, 2 estaciones, 136 utiles.
    ahora = datetime(2026, 8, 31, 18, 0, tzinfo=UTC).astimezone(settings.tz)
    i, motivo = C.plan_interval(C.RESERVA + 136, 2, ahora, priority=False)
    assert i == pytest.approx(6 * 3600 * 2 / 136, abs=1) and "136 llamadas para 6.0 h" in motivo


def test_franja_solo_decide_el_ritmo():
    """Dentro de la franja (+-30 min) hay prioridad; fuera no, pero la
    estacion se sigue estudiando."""
    assert C._in_window(RUTA, _lunes(8))
    assert C._in_window(RUTA, _lunes(6, 31)) and C._in_window(RUTA, _lunes(10, 29))
    assert not C._in_window(RUTA, _lunes(14))
    assert not C._in_window(RUTA, _lunes(6, 29))
    assert not C._in_window(RUTA, _lunes(8) + timedelta(days=5))       # sabado
    assert list(C.targets([RUTA])) == [STOP]


def test_franja_que_cruza_medianoche():
    """Como en pick_active_route: 23:00-01:00 del viernes sigue siendo del
    viernes el sabado a las 00:30."""
    noche = {"days": [4], "time_from": "23:00", "time_to": "01:00"}
    viernes = _lunes(23, 30) + timedelta(days=4)
    assert C._in_window(noche, viernes)
    assert C._in_window(noche, viernes + timedelta(hours=1))           # sabado 00:30
    assert C._in_window(noche, viernes.replace(hour=22, minute=40))    # margen
    assert not C._in_window(noche, viernes + timedelta(hours=3))       # sabado 02:30
    assert not C._in_window(noche, viernes - timedelta(days=1))        # jueves
    # Una franja hasta las 23:59 con margen alcanza a la madrugada siguiente.
    tarde = {"days": [0], "time_from": "20:00", "time_to": "23:59"}
    assert C._in_window(tarde, _lunes(23, 50) + timedelta(minutes=30))


# =====================================================================
#  Pasadas de verdad contra el PRIM falso
# =====================================================================

async def test_recolector_no_toca_la_via_nueva(pc, fake_prim, make_route):
    """Fallo 18.3.11: el recolector ve la via antes que la pantalla; la
    pantalla la tiene que cantar igual (platform_new)."""
    from app import board
    from app.api import common
    make_route()
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6, platform="21",
                               train="135711", jid="SNCF:VJ:J:135711"),
                  Dep("C01739", "Gare Saint-Lazare", 9, platform="4", train="135800"))
    res = await C.sample_once(now=_lunes(14))
    assert res["recorded"] == 2 and res["stations"] == 1     # los dos sentidos
    assert board._seen_platforms == {}
    data = await common.board(None, False)
    dep = data["legs"][0]["departures"][0]
    assert dep["platform"] == "21" and dep["platform_new"] is True
    # Ni la pasada siguiente ni otro tablero la vuelven a cantar.
    await C.sample_once(now=_lunes(14))
    assert (await common.board(None, False))["legs"][0]["departures"][0]["platform_new"] is False


async def test_recolector_sqlite_en_un_hilo_y_sin_get_route(pc, fake_prim, make_route,
                                                            monkeypatch):
    """§18.4: leer las rutas y apuntar andenes va a un hilo (el bucle de
    eventos es el de las peticiones del iPhone) y no se pide cada ruta otra
    vez con get_route."""
    from app import db, platform
    make_route()
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6, platform="21",
                               train="135711"))
    bucle = threading.get_ident()
    hilos: dict[str, int] = {}

    def no_se_usa(*a, **k):
        raise AssertionError("get_route de mas en el recolector")

    real_list, real_learn = db.list_routes, platform.learn_many

    def list_routes():
        hilos["list_routes"] = threading.get_ident()
        return real_list()

    def learn_many(*a, **k):
        hilos["learn_many"] = threading.get_ident()
        return real_learn(*a, **k)

    monkeypatch.setattr(db, "get_route", no_se_usa)
    monkeypatch.setattr(db, "list_routes", list_routes)
    monkeypatch.setattr(platform, "learn_many", learn_many)
    res = await C.sample_once(now=_lunes(14))
    assert res["recorded"] == 1
    assert set(hilos) == {"list_routes", "learn_many"}
    assert bucle not in hilos.values()


async def test_recolector_respeta_la_reserva_y_la_noche(pc, fake_prim, make_route):
    """R78 de punta a punta: con la reserva justa o de madrugada, ninguna
    llamada; sin rutas de tren, tampoco."""
    make_route()
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6, platform="21"))
    pc.quota_counter.remaining = lambda ep: C.RESERVA
    res = await C.sample_once(now=_lunes(14))
    assert res["interval"] == 0 and res["remaining"] == C.RESERVA
    del pc.quota_counter.remaining
    res = await C.sample_once(now=_lunes(3))
    assert res["interval"] == 0 and res["reason"].startswith("horas muertas")
    assert fake_prim.calls.get("stop-monitoring", 0) == 0
    res = await C.sample_once(now=_lunes(14))
    assert res["recorded"] == 1 and fake_prim.calls["stop-monitoring"] == 1


async def test_recolector_estacion_caida_no_para_el_resto(pc, fake_prim, make_route):
    otra = ruta_j(name="otra", legs=[dict(ruta_j()["legs"][0],
                                          from_id="stop_area:IDFM:65063")])
    make_route()
    make_route(otra)
    fake_prim.add("65063", Dep("C01739", "Ermont - Eaubonne", 6, platform="2", train="1"))
    # Un 404 es de esa estacion, no de PRIM entero (un timeout pausaria el
    # endpoint y con razon: PRIM estaria caido para todas).
    fake_prim.fail_stations["71370"] = 404
    res = await C.sample_once(now=_lunes(14))
    assert res["stations"] == 2 and res["recorded"] == 1


def test_estado_para_health(env):
    col = C.Collector()
    st = col.status()
    assert st["running"] is False and st["reason"] == "sin arrancar"
    assert set(st) == {"enabled", "running", "last_at", "session_total", "stations",
                       "recorded", "reason", "interval", "remaining", "priority"}
