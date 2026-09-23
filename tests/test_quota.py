"""Cuota de PRIM por endpoint y dia UTC, persistida, con niveles y
degradacion elegante (R88)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from jsonschema import Draft202012Validator

DOCS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contract")


def _validator(name: str) -> Draft202012Validator:
    with open(os.path.join(DOCS, "openapi.yaml"), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}",
                                 "components": spec["components"]})


class Reloj:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt


@pytest.fixture
def bd(env):
    from app import db
    db.init()
    return env


def _cuota(cap=100, key="clave-a", reloj=None):
    from app.quota import Quota
    return Quota(cap, key=key, clock=reloj)


def _gastar(q, ep, n):
    for _ in range(n):
        q.spend(ep)


def test_quota_niveles_y_degradacion(bd):
    """< 70 % ok, < 85 % warn, < 95 % critical y si no exhausted; el global
    es el del peor endpoint y el TTL se alarga con el nivel."""
    q = _cuota(100)
    ep = "stop-monitoring"
    esperado = [(69, "ok", 30, 25), (70, "warn", 60, 50), (84, "warn", 60, 50),
                (85, "critical", 120, 100), (94, "critical", 120, 100),
                (95, "exhausted", 300, 600), (100, "exhausted", 300, 600)]
    hechas = 0
    for n, nivel, hint, ttl in esperado:
        _gastar(q, ep, n - hechas)
        hechas = n
        assert q.used(ep) == n
        assert q.level(ep) == nivel and q.level() == nivel
        assert q.refresh_hint() == hint
        assert q.ttl_for(25, ep) == ttl
    assert q.can_spend(ep) is False and q.remaining(ep) == 0
    # Los otros endpoints siguen a lo suyo.
    assert q.level("navitia") == "ok" and q.can_spend("navitia")
    assert q.ttl_for(600, "navitia") == 600


def test_la_cabecera_manda_si_es_mas_pesimista(bd):
    q = _cuota(1000)
    q.spend("stop-monitoring", remaining_reported=100)   # 1 hecha, PRIM dice 100
    assert q.remaining("stop-monitoring") == 100
    assert q.level("stop-monitoring") == "critical"      # 90 % gastado
    q.spend("stop-monitoring", remaining_reported=990)   # PRIM mas optimista
    assert q.remaining("stop-monitoring") == 990
    q.mark_exhausted("general-message")                  # 429 del dia
    assert q.can_spend("general-message") is False
    assert q.level() == "exhausted"


def test_refresco_por_los_endpoints_del_tablero(bd):
    """Si el planificador agota navitia, el tablero no se refresca mas lento."""
    q = _cuota(10)
    _gastar(q, "navitia", 10)
    assert q.level() == "exhausted" and q.level("navitia") == "exhausted"
    assert q.refresh_hint() == 30
    _gastar(q, "general-message", 8)
    assert q.refresh_hint() == 60


def test_dia_utc_nuevo_empieza_de_cero(bd):
    reloj = Reloj(datetime(2026, 9, 24, 23, 59, 30, tzinfo=timezone.utc))
    q = _cuota(100, reloj=reloj)
    _gastar(q, "stop-monitoring", 99)
    q.spend("stop-monitoring", remaining_reported=0)
    assert q.can_spend("stop-monitoring") is False
    snap = q.snapshot()
    assert snap["day_utc"] == "2026-09-24"
    assert snap["resets_at"] == "2026-09-25T00:00:00+00:00"

    reloj.dt = datetime(2026, 9, 25, 0, 0, 5, tzinfo=timezone.utc)
    assert q.can_spend("stop-monitoring") is True
    assert q.used("stop-monitoring") == 0 and q.level() == "ok"
    assert q.snapshot()["day_utc"] == "2026-09-25"
    assert q.as_legacy() == {}                    # ya no ensena la cifra de ayer
    # Ayer quedo guardado para el historial.
    dias = {(h["day_utc"], h["endpoint"]): h["used"] for h in q.history(2)}
    assert dias[("2026-09-24", "stop-monitoring")] == 100
    assert dias[("2026-09-25", "stop-monitoring")] == 0


def test_hora_de_paris_no_manda(bd):
    """01:30 en Paris del 25 son las 23:30 UTC del 24: sigue siendo el 24."""
    reloj = Reloj(datetime(2026, 9, 25, 1, 30, tzinfo=timezone(timedelta(hours=2))))
    q = _cuota(100, reloj=reloj)
    assert q.snapshot()["day_utc"] == "2026-09-24"


def test_tope_configurable(bd, monkeypatch):
    from app import quota
    from app.config import settings
    monkeypatch.setenv("TRAJET_QUOTA_CAP", "800")
    settings.reload()
    q = quota.get_quota()
    assert q.cap == 800
    assert all(e["cap"] == 800 for e in q.snapshot()["endpoints"])
    assert quota.get_quota() is q
    monkeypatch.setenv("TRAJET_QUOTA_CAP", "5")
    settings.reload()
    q5 = quota.get_quota()
    assert q5 is not q and q5.cap == 5
    _gastar(q5, "navitia", 5)
    assert q5.can_spend("navitia") is False


def test_clave_nueva_contador_nuevo(bd):
    q = _cuota(100, key="clave-a")
    _gastar(q, "stop-monitoring", 7)
    q.reset_for_new_key("clave-b")
    assert q.used("stop-monitoring") == 0
    _gastar(q, "stop-monitoring", 2)
    q.reset_for_new_key("clave-a")                # vuelve la de antes: su cuenta
    assert q.used("stop-monitoring") == 7
    # El historial suma todas las claves del dia.
    hoy = q.snapshot()["day_utc"]
    fila = [h for h in q.history(1) if h["endpoint"] == "stop-monitoring"]
    assert fila == [{"day_utc": hoy, "endpoint": "stop-monitoring", "used": 9}]


def test_persistida_sobrevive_a_un_reinicio(bd):
    from app import db
    from app.quota import key_id
    q = _cuota(100, key="clave-a")
    _gastar(q, "general-message", 3)
    q.spend("general-message", remaining_reported=940)
    otra = _cuota(100, key="clave-a")
    assert otra.used("general-message") == 4
    assert otra.snapshot()["endpoints"][1]["remaining_reported"] == 940
    with db.conn() as c:
        filas = c.execute("SELECT key_id FROM quota_usage").fetchall()
    # En la BD solo un hash corto, nunca la clave.
    assert [r["key_id"] for r in filas] == [key_id("clave-a")]
    assert "clave-a" not in key_id("clave-a")


def test_historial_y_contrato(bd):
    reloj = Reloj(datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc))
    q = _cuota(100, reloj=reloj)
    for dia, n in ((20, 5), (22, 7), (24, 1)):
        reloj.dt = datetime(2026, 9, dia, 12, 0, tzinfo=timezone.utc)
        _gastar(q, "stop-monitoring", n)
    hist = q.history(7)
    assert len(hist) == 7 * 3
    assert hist[0]["day_utc"] == "2026-09-18" and hist[-1]["day_utc"] == "2026-09-24"
    sm = {h["day_utc"]: h["used"] for h in hist if h["endpoint"] == "stop-monitoring"}
    assert sm == {"2026-09-18": 0, "2026-09-19": 0, "2026-09-20": 5, "2026-09-21": 0,
                  "2026-09-22": 7, "2026-09-23": 0, "2026-09-24": 1}
    v = _validator("AdminQuotaDay")
    for h in hist:
        v.validate(h)
    _validator("QuotaV1").validate(q.snapshot())
    _validator("AdminQuota").validate({"today": q.snapshot(), "history": hist})


def test_sin_bd_no_rompe(env):
    """Sin tabla (BD sin migrar) cuenta en memoria y no lanza nada."""
    q = _cuota(10)
    _gastar(q, "navitia", 3)
    assert q.used("navitia") == 3
    assert [h["used"] for h in q.history(1) if h["endpoint"] == "navitia"] == [3]


async def test_ttl_degradado_sirve_la_copia_sin_llamar(bd, fake_prim):
    """R88: en warn un dato de 30 s todavia vale (TTL 25 x2)."""
    from app import prim
    await prim.startup()
    try:
        c = prim.get_client()
        await c.stop_monitoring("STIF:StopArea:SP:71370:")
        assert fake_prim.calls["stop-monitoring"] == 1
        for entry in c._cache.values():
            entry.fetched_at -= 30
        _gastar(c.quota_counter, "stop-monitoring", 700)       # 70 %: warn
        _, age = await c.stop_monitoring("STIF:StopArea:SP:71370:")
        assert fake_prim.calls["stop-monitoring"] == 1 and age >= 30
        assert prim.server_state()["degraded"] is True
        assert prim.server_state()["refresh_hint_s"] == 60
    finally:
        await prim.shutdown()


async def test_sin_cuota_no_se_llama(bd, fake_prim):
    """Tope alcanzado: copia de cualquier edad o PrimError('quota'), sin
    llamar a PRIM."""
    from app import prim
    from app.prim import PrimError
    await prim.startup()
    try:
        c = prim.get_client()
        await c.stop_monitoring("STIF:StopArea:SP:71370:")
        for entry in c._cache.values():
            entry.fetched_at -= 86400
        _gastar(c.quota_counter, "stop-monitoring", c.quota_counter.cap)
        data, age = await c.stop_monitoring("STIF:StopArea:SP:71370:")
        assert age >= 86400 and data
        with pytest.raises(PrimError) as e:
            await c.stop_monitoring("STIF:StopArea:SP:65063:")
        assert e.value.kind == "quota"
        assert fake_prim.calls["stop-monitoring"] == 1
    finally:
        await prim.shutdown()


async def test_recolector_vuelve_a_aprender_tras_medianoche(bd, fake_prim, make_route):
    """Fallo 18.3.8: ayer la cuota quedo por debajo de la reserva; hoy el
    recolector vuelve a muestrear aunque nadie haya abierto la app."""
    from app import collector, prim
    from app.config import settings
    from fakeprim import Dep
    make_route()
    fake_prim.add("71370", Dep("C01739", "Ermont - Eaubonne", 6, platform="21",
                               train="135711"))
    await prim.startup()
    try:
        q = prim.get_client().quota_counter
        reloj = Reloj(datetime.now(timezone.utc) - timedelta(days=1))
        q._clock = reloj
        _gastar(q, "stop-monitoring", q.cap - 100)          # quedan 100 < 320
        manana = datetime.now(settings.tz).replace(hour=7, minute=0, second=0,
                                                   microsecond=0)
        ayer = await collector.sample_once(now=manana)
        assert ayer["interval"] == 0 and ayer["remaining"] == 100

        reloj.dt = datetime.now(timezone.utc)                # medianoche UTC pasada
        hoy = await collector.sample_once(now=manana)
        assert hoy["remaining"] == q.cap and hoy["interval"] > 0
        assert fake_prim.calls["stop-monitoring"] == 1
        assert hoy["recorded"] == 1
    finally:
        await prim.shutdown()
