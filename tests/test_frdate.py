"""Avisos de hoy frente a obras de mas adelante (app/frdate.py): la parte de
R28 y R69 que lee las fechas del texto en frances, y el «hoy» de Paris
(fallo 18.3.19 de docs/servidor.md)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app import frdate

HOY = date(2026, 9, 1)


@pytest.mark.parametrize("texto", [
    # En presente: pasa AHORA aunque cite fechas futuras.
    "Trafic interrompu entre Nation et Vincennes, reprise estimée le 3 septembre.",
    "Le trafic est interrompu jusqu'à nouvel ordre. Travaux le 20 septembre.",
    "Actuellement, travaux sur la ligne. Fin prévue le 30 septembre.",
    "Trafic perturbé en raison de la grève du 15 septembre.",
    "En raison de la grève, le trafic est perturbé.",
    "Temps d'attente allongé, retour à la normale le 10 octobre.",
    "Ce soir, pas de trains après 22h.",
    "Aujourd'hui et le 12 septembre, bus de remplacement.",
])
def test_presente_es_activo(texto):
    assert frdate.starts_later(texto, HOY) is None


def test_fecha_futura_es_planificado():
    """R28/R69: «le 24 septembre» visto el 1 de septiembre = obras futuras."""
    t = "Travaux : le 24 septembre, pas de trains entre Haussmann et Magenta."
    assert frdate.starts_later(t, HOY) == date(2026, 9, 24)
    assert frdate.starts_later("Du 1er octobre au 5 octobre, arrêt non desservi.", HOY) \
        == date(2026, 10, 1)
    # La primera fecha (la mas temprana) manda.
    assert frdate.starts_later("Les 30 septembre et 12 septembre, travaux.", HOY) \
        == date(2026, 9, 12)


def test_jusqua_antes_de_la_fecha_es_activo():
    """«jusqu'au 15 septembre» = ya ha empezado."""
    assert frdate.starts_later("Travaux jusqu'au 15 septembre inclus.", HOY) is None
    assert frdate.starts_later("Arrêt fermé jusqu'à 23h le 20 septembre.", HOY) is None


def test_fecha_de_hoy_o_pasada_es_activo():
    assert frdate.starts_later("Travaux le 1er septembre, pas de trains.", HOY) is None
    assert frdate.starts_later("Travaux du 20 août au 30 septembre.", HOY) is None


def test_ano_mas_cercano():
    """Los avisos no llevan año: nunca mas de 60 dias atras."""
    dic = date(2026, 12, 20)
    assert frdate.starts_later("Travaux le 5 janvier, bus de remplacement.", dic) \
        == date(2027, 1, 5)
    # 43 dias atras: es de este año, ya paso, y el aviso esta activo.
    assert frdate.starts_later("Travaux depuis le 20 juillet.", HOY) is None
    # 29 de febrero: solo existe en años bisiestos (2028).
    assert frdate.starts_later("Travaux le 29 février.", date(2027, 12, 1)) == date(2028, 2, 29)


@pytest.mark.parametrize("texto", ["", None, "Travaux sur la ligne.", "Le 45 septembre."])
def test_sin_fecha_es_activo(texto):
    """Ante la duda, activo: mejor un aviso de mas que callar una interrupcion."""
    assert frdate.starts_later(texto, HOY) is None


def test_mayusculas_y_meses_sin_tilde():
    assert frdate.starts_later("TRAVAUX LE 24 SEPTEMBRE", HOY) == date(2026, 9, 24)
    assert frdate.starts_later("Travaux le 3 fevrier.", date(2027, 1, 10)) == date(2027, 2, 3)
    assert frdate.starts_later("Travaux le 8 decembre.", HOY) == date(2026, 12, 8)


def test_hoy_es_el_de_paris(monkeypatch):
    """Fallo 18.3.19: a la 01:30 de Paris (23:30 UTC del dia anterior) un
    aviso «le 24 septembre» del mismo dia 24 ya es de hoy, no de mañana."""
    fijo = datetime(2026, 9, 23, 23, 30, tzinfo=timezone.utc)      # 24/09 01:30 Paris

    class Reloj(datetime):
        @classmethod
        def now(cls, tz=None):
            return fijo.astimezone(tz) if tz else fijo.replace(tzinfo=None)

    monkeypatch.setattr(frdate, "datetime", Reloj)
    assert frdate.today_paris() == date(2026, 9, 24)
    t = "Du 24 septembre au 27 septembre inclus, l'arrêt ne sera pas desservi."
    assert frdate.starts_later(t) is None                    # activo: es hoy
    assert frdate.starts_later(t, date(2026, 9, 23)) == date(2026, 9, 24)
