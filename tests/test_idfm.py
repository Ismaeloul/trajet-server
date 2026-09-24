"""Identificadores, avisos por linea, via real, destinos y orden de lineas
(app/idfm.py): R62, R70, R72, R73.

Sin red ni BD: son funciones puras. El orden de lineas se comprueba ademas
por la API (/api/stops/{id}/lines), como hacia tools/test_plan.py contra la
API real.
"""
from __future__ import annotations

import pytest

from app import idfm

# ---------------- identificadores ----------------


def test_ids_navitia_y_siri():
    """Se guarda el id de Navitia y se traduce a SIRI al vuelo."""
    assert idfm.sa_code("stop_area:IDFM:71370") == "71370"
    assert idfm.sa_code("STIF:StopArea:SP:71370:") == "71370"
    assert idfm.sa_code("") == ""
    assert idfm.sa_to_siri("stop_area:IDFM:71370") == "STIF:StopArea:SP:71370:"
    assert idfm.line_code("line:IDFM:C01739") == "C01739"
    assert idfm.line_code("STIF:Line::C01739:") == "C01739"
    assert idfm.line_code("") == ""
    assert idfm.line_to_siri("line:IDFM:C01739") == "STIF:Line::C01739:"
    assert idfm.line_to_navitia("STIF:Line::C01739:") == "line:IDFM:C01739"


def test_first_value_lista_u_objeto():
    """SIRI alterna {'value': x} y [{'value': x}] en los mismos campos."""
    assert idfm.first_value({"value": "J"}) == "J"
    assert idfm.first_value([{"value": "J"}, {"value": "L"}]) == "J"
    assert idfm.first_value([]) is None
    assert idfm.first_value(None) is None
    assert idfm.first_value("J") == "J"


# ---------------- R70: a que lineas afecta un aviso ----------------

def test_lineas_en_mensaje():
    """R70: la linea sale de Content.LineRef O de IDFM.Cnnnnn en el
    ItemIdentifier (solo 22 de 328 avisos traen LineRef)."""
    con_lineref = {"ItemIdentifier": "IDFM:MSG:1",
                   "Content": {"LineRef": [{"value": "STIF:Line::C01383:"},
                                           {"value": "STIF:Line::C01384:"}]}}
    assert idfm.lines_in_message(con_lineref) == {"C01383", "C01384"}

    en_item = {"ItemIdentifier": "RATP:MSG.SAE-BUS.IDFM.C01199.IDFM.C00306.7"}
    assert idfm.lines_in_message(en_item) == {"C01199", "C00306"}
    assert idfm.lines_in_message({"ItemIdentifier": "x:IDFM:C01739:y"}) == {"C01739"}

    ambos = {"ItemIdentifier": "SNCF:MSG.IDFM.C01739.1",
             "Content": {"LineRef": [{"value": "STIF:Line::C01743:"}]}}
    assert idfm.lines_in_message(ambos) == {"C01739", "C01743"}

    # Sin linea identificable no afecta a ninguna.
    assert idfm.lines_in_message({"ItemIdentifier": "IDFM:MSG:9", "Content": {}}) == set()


@pytest.mark.parametrize("msg", [
    {"Content": None, "ItemIdentifier": None},
    {"Content": {"LineRef": None}},
    {"Content": "texto suelto"},
    {"Content": {"LineRef": ["STIF:Line::C01383:", None, 5]}},
    {"ItemIdentifier": {"value": "IDFM.C01383"}},
    {},
])
def test_lineas_en_mensaje_con_campos_raros_no_revienta(msg):
    """Un aviso con Content null, LineRef suelto o de otro tipo degrada."""
    assert isinstance(idfm.lines_in_message(msg), set)


def test_lineas_en_mensaje_lineref_suelto_o_texto():
    assert idfm.lines_in_message({"Content": {"LineRef": {"value": "STIF:Line::C01383:"}}}) \
        == {"C01383"}
    assert idfm.lines_in_message({"Content": {"LineRef": ["STIF:Line::C01383:"]}}) == {"C01383"}


# ---------------- R73: via real ----------------

@pytest.mark.parametrize("valor, via", [
    (None, None), ("", None), ("   ", None),
    ("unknown", None), ("UNKNOWN", None), ("none", None), ("null", None),
    ({"value": "unknown"}, None), ([], None), ([{"value": None}], None),
    ("PARIS NORD", None), ("Paris Saint-Lazare", None), ("PARIS LYON", None),
    ("PARIS MONTPARNASSE", None), ("PARIS AUSTERLITZ", None), ("PARIS BERCY", None),
    ("PARIS EST", None),
    # Captura real de Argenteuil (tests/fixtures/prim): el nombre de la estacion.
    ("ARGENTEUIL", None), ({"value": "ARGENTEUIL"}, None), ("Ermont-Eaubonne", None),
    (True, None),
    ("21", "21"), ({"value": "21"}, "21"), ([{"value": "11"}], "11"), (7, "7"),
    (" 21 ", "21"), ("A", "A"), ("3B", "3B"), ("BIS", "BIS"), ("Voie 2", "Voie 2"),
])
def test_real_platform(valor, via):
    """R73: fuera 'unknown', vacio y nombres de estacion; una via es corta o
    lleva cifras."""
    assert idfm.real_platform(valor) == via


# ---------------- R72: destinos comparables ----------------

def test_norm_text_destinos():
    """R72: sin tildes, con los guiones unificados y sin espacios finos ni
    duros: si no, el filtro por direccion deja el tablero vacio."""
    base = idfm.norm_text("Ermont - Eaubonne")
    for variante in ("Ermont – Eaubonne", "Ermont — Eaubonne", "Ermont ‐ Eaubonne",
                     "ERMONT - EAUBONNE", "Ermont - Eaubonne",
                     "Ermont - Eaubonne", "Ermont -  Eaubonne",
                     "  Ermont - Eaubonne  "):
        assert idfm.norm_text(variante) == base, repr(variante)
    assert idfm.norm_text("Châtillon Montrouge") == idfm.norm_text("CHATILLON MONTROUGE")
    assert idfm.norm_text("Église de Pantin") == "eglise de pantin"
    assert idfm.norm_text("Straße") == "strasse"        # casefold, no lower
    assert idfm.norm_text("") == "" and idfm.norm_text(None) == ""
    assert idfm.norm_text("La Défense") != idfm.norm_text("La Défense (Puteaux)")


# ---------------- modos ----------------

@pytest.mark.parametrize("modo, publica", [
    ("RER", True), ("Train", True), ("Train Transilien", True), ("TER", True),
    ("Métro", False), ("Metro", False), ("Bus", False), ("Tramway", False),
    ("Funiculaire", False), ("Navette", False), ("", False), ("Barco", False),
])
def test_publishes_platform(modo, publica):
    """R3: solo tren, RER, Transilien y TER publican via."""
    assert idfm.publishes_platform(modo) is publica


def test_code_sort_key_numerico():
    codes = ["14", "3", "3bis", "12", "A", "T2", "13", "N52"]
    assert sorted(codes, key=idfm.code_sort_key) == ["3", "3bis", "12", "13", "14",
                                                     "A", "N52", "T2"]


# ---------------- R62: lineas de una parada ordenadas por modo ----------------

# Saint-Lazare de verdad: 38 lineas y 30 son buses. Desordenadas a proposito.
_SAINT_LAZARE = [
    {"id": "line:IDFM:C00020", "code": "20", "name": "20", "mode": "Bus"},
    {"id": "line:IDFM:C01742", "code": "A", "name": "RER A", "mode": "RER"},
    {"id": "line:IDFM:C01386", "code": "14", "name": "14", "mode": "Métro"},
    {"id": "line:IDFM:C01390", "code": "T2", "name": "T2", "mode": "Tramway"},
    {"id": "line:IDFM:C00094", "code": "94", "name": "94", "mode": "Bus"},
    {"id": "line:IDFM:C01371", "code": "3", "name": "3", "mode": "Métro"},
    {"id": "line:IDFM:C09999", "code": "J", "name": "Bus de remplacement J", "mode": "Bus"},
    {"id": "line:IDFM:C01739", "code": "J", "name": "J", "mode": "Train Transilien"},
    {"id": "line:IDFM:C01383", "code": "13", "name": "13", "mode": "Métro"},
    {"id": "line:IDFM:C00021", "code": "21", "name": "21", "mode": "Bus"},
    {"id": "line:IDFM:C01382", "code": "12", "name": "12", "mode": "Métro"},
    {"id": "line:IDFM:C01743", "code": "E", "name": "RER E", "mode": "RER"},
    {"id": "line:IDFM:C00300", "code": "TER", "name": "TER Normandie", "mode": "TER"},
]


def test_lines_orden_por_modo():
    """R62: metro, RER, tren, TER, tranvia y buses; en orden numerico; los
    buses de sustitucion, los ultimos."""
    orden = sorted(_SAINT_LAZARE, key=idfm.mode_sort_key)
    assert [(x["mode"], x["code"]) for x in orden] == [
        ("Métro", "3"), ("Métro", "12"), ("Métro", "13"), ("Métro", "14"),
        ("RER", "A"), ("RER", "E"), ("Train Transilien", "J"), ("TER", "TER"),
        ("Tramway", "T2"), ("Bus", "20"), ("Bus", "21"), ("Bus", "94"),
        ("Bus", "J")]
    assert "remplacement" in orden[-1]["name"]


def test_lines_orden_por_modo_en_la_api(client, fake_prim):
    """Lo que comprobaba tools/test_plan.py contra Saint-Lazare: el metro
    sale primero, no el bus; 3 antes que 12; la sustitucion al final."""
    fake_prim.lines_at["stop_area:IDFM:71370"] = [
        {"id": x["id"], "code": x["code"], "name": x["name"],
         "commercial_mode": {"name": x["mode"]}, "color": "000000"} for x in _SAINT_LAZARE]
    r = client.get("/api/stops/stop_area:IDFM:71370/lines")
    assert r.status_code == 200
    lineas = r.json()["lines"]
    assert all("tro" in x["mode"] for x in lineas[:4])
    assert [x["code"] for x in lineas if "tro" in x["mode"]] == ["3", "12", "13", "14"]
    assert "remplacement" in lineas[-1]["name"]
    assert fake_prim.calls == {"navitia": 1}
