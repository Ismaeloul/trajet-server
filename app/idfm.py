"""Conversion de identificadores entre Navitia y SIRI.

El portal PRIM usa dos sistemas de IDs para las mismas cosas:
  Navitia: stop_area:IDFM:71370      line:IDFM:C01739
  SIRI:    STIF:StopArea:SP:71370:   STIF:Line::C01739:

Guardamos siempre el de Navitia en la base de datos (es el que devuelve el
buscador de paradas) y convertimos al vuelo cuando hablamos con SIRI.
"""
import re
import unicodedata

_LINE_IN_ITEM = re.compile(r"IDFM[.:](C\d{5})")


def sa_code(value: str) -> str:
    """Extrae el codigo de zona de cualquiera de las dos formas."""
    if not value:
        return ""
    return value.rstrip(":").rsplit(":", 1)[-1]


def sa_to_siri(value: str) -> str:
    """stop_area:IDFM:71370 -> STIF:StopArea:SP:71370:"""
    return f"STIF:StopArea:SP:{sa_code(value)}:"


def line_code(value: str) -> str:
    """line:IDFM:C01739 o STIF:Line::C01739: -> C01739"""
    if not value:
        return ""
    return value.rstrip(":").rsplit(":", 1)[-1]


def line_to_siri(value: str) -> str:
    """line:IDFM:C01739 -> STIF:Line::C01739:"""
    return f"STIF:Line::{line_code(value)}:"


def line_to_navitia(value: str) -> str:
    """STIF:Line::C01739: -> line:IDFM:C01739"""
    return f"line:IDFM:{line_code(value)}"


def lines_in_message(msg: dict) -> set:
    """Codigos de linea a los que afecta un InfoMessage de general-message.

    Solo 22 de 328 mensajes traen Content.LineRef; en el resto la linea va
    embebida en el ItemIdentifier (p.ej. '...MSG.SAE-BUS.IDFM.C01199....').
    Combinando ambos se identifica el 99,7% de los avisos.
    """
    out = set()
    # SIRI a veces manda `"Content": null` o un LineRef suelto en vez de una
    # lista: un aviso raro no puede tumbar el indice de avisos entero.
    content = msg.get("Content") or {}
    refs = content.get("LineRef") if isinstance(content, dict) else None
    if isinstance(refs, dict):
        refs = [refs]
    for ref in refs or []:
        code = line_code(str(first_value(ref) or ""))
        if code:
            out.add(code)
    item = msg.get("ItemIdentifier")
    if isinstance(item, dict):
        item = item.get("value")
    out.update(_LINE_IN_ITEM.findall(item if isinstance(item, str) else ""))
    return out


def first_value(node):
    """SIRI alterna {'value': x} y [{'value': x}] en los mismos campos."""
    if isinstance(node, list):
        node = node[0] if node else None
    if isinstance(node, dict):
        return node.get("value")
    return node


# Nombres de estacion que PRIM manda en el campo de la via (sondeo del 30/08).
_STATION_NAMES = {"PARIS NORD", "PARIS SAINT-LAZARE", "PARIS EST", "PARIS LYON",
                  "PARIS MONTPARNASSE", "PARIS AUSTERLITZ", "PARIS BERCY"}

# Una via es un numero ("21"), una letra ("A") o una mezcla corta ("3B").
# Una palabra de mas de 3 letras sin ninguna cifra es el nombre de la
# estacion: la captura real de Argenteuil (tests/fixtures/prim, 23/09) trae
# 'ARGENTEUIL' como DeparturePlatformName en todos los trenes, y con solo la
# lista de Paris eso salia en pantalla como «Vía ARGENTEUIL».
_MAX_LETTERS_PLATFORM = 3


def real_platform(value) -> str | None:
    """Devuelve el anden solo si es un anden de verdad.

    Observado en la API: el campo llega ausente (bus, metro), con el literal
    'unknown' (Transilien en Saint-Lazare) o con el nombre de la estacion
    ('PARIS NORD' en el RER B, 'ARGENTEUIL' en Argenteuil). Ninguno es una via.
    """
    v = first_value(value)
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    v = " ".join(str(v).split())
    if not v or v.lower() in ("unknown", "none", "null"):
        return None
    if v.upper() in _STATION_NAMES:
        return None
    if len(v) > _MAX_LETTERS_PLATFORM and not any(c.isdigit() for c in v):
        return None
    return v


def norm_text(value: str) -> str:
    """Normaliza un nombre de destino para poder compararlo con fiabilidad.

    Los destinos de IDFM traen espacios finos y duros (U+2009, U+00A0),
    guiones de varios tipos y acentos. Comparar cadenas tal cual hace que el
    filtro por direccion deje de casar por un caracter invisible y el tablero
    salga vacio sin decir por que. Se quitan acentos, se unifican los guiones
    y se colapsa cualquier espacio.
    """
    if not value:
        return ""
    v = unicodedata.normalize("NFKD", str(value))
    v = "".join(c for c in v if not unicodedata.combining(c))
    for dash in ("‐", "‑", "‒", "–", "—", "―"):
        v = v.replace(dash, "-")
    v = " ".join(v.split())
    return v.casefold()


# Orden en que quiero ver las lineas al montar un tramo. Saint-Lazare devuelve
# 38 lineas y 30 son buses: ordenando por nombre de modo, "Bus" gana a "Metro"
# y el metro queda enterrado 30 filas mas abajo. En la practica parecia que
# solo hubiera buses.
_MODE_ORDER = (
    ("metro",), ("rer",), ("train", "transilien"), ("ter",),
    ("tram",), ("funicular", "funiculaire"), ("navette", "cable"),
)


def publishes_platform(mode: str) -> bool:
    """El modo publica via (tren, RER, Transilien, TER).

    Medido el 30/08: metro, bus y tranvia no publican via jamas (0 de ~600).
    Un modo desconocido cuenta como que no: asi no se reserva un hueco vacio
    (R3). Si aun asi llega una via, se ensena igual.
    """
    return mode_rank(mode) in (1, 2, 3)


def mode_rank(mode: str) -> int:
    m = norm_text(mode)
    for i, keys in enumerate(_MODE_ORDER):
        if any(k in m for k in keys):
            return i
    return len(_MODE_ORDER)          # el bus, y lo que no reconozca, al final


def code_sort_key(code) -> tuple:
    """Ordena '3' antes que '12', y dentro de las letras, alfabetico."""
    c = str(code or "").strip()
    head = ""
    i = 0
    while i < len(c) and not c[i].isdigit():
        head += c[i]
        i += 1
    num = ""
    while i < len(c) and c[i].isdigit():
        num += c[i]
        i += 1
    return (head.casefold(), int(num) if num else 0, c.casefold())


def mode_sort_key(line: dict) -> tuple:
    """Metro y trenes primero; los buses de sustitucion, los ultimos."""
    name = norm_text(line.get("name") or "")
    replacement = 1 if "remplacement" in name or "substitution" in name else 0
    return (replacement, mode_rank(line.get("mode") or ""),
            code_sort_key(line.get("code")))
