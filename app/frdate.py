"""Distinguir un aviso que me afecta AHORA de uno programado para dentro de un mes.

El endpoint general-message no dice cuando empieza una perturbacion: solo trae
ValidUntilTime, y en la practica viene muy holgado. Resultado: un aviso de obras
"del 24 al 27 de septiembre" marcaba la linea 13 como perturbada el 30 de agosto.

Como el unico dato de inicio esta en el texto libre en frances, hay que leerlo de
ahi. Es una heuristica, no una ciencia, asi que ante la duda se considera ACTIVO:
mas vale un aviso de mas que callarse una interrupcion real.
"""
import re
from datetime import date, timedelta

MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
}

_DATE_RX = re.compile(
    r"\b(\d{1,2})\s*(?:er)?\s+("
    + "|".join(sorted(MONTHS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE)

# Si el texto habla en presente, da igual que mencione fechas futuras:
# "trafic interrompu, reprise estimee le 3 septembre" pasa AHORA.
_NOW_RX = re.compile(
    r"actuellement|en\s+cours|reprise\s+estim|trafic\s+interrompu"
    r"|est\s+interrompu|sont\s+interrompu|trafic\s+perturb|est\s+perturb"
    r"|ce\s+soir|aujourd'hui|ce\s+matin|en\s+raison\s+d[eu]\s+la\s+greve"
    r"|temps\s+d'attente|retard",
    re.IGNORECASE)

# "jusqu'au 15 septembre" = ya ha empezado y termina ese dia
_UNTIL_RX = re.compile(r"jusqu'(?:au|a|à)", re.IGNORECASE)


def _to_date(day: int, month: int, today: date) -> date | None:
    """Los avisos no llevan año. Se elige el mas cercano al presente."""
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            cand = date(year, month, day)
        except ValueError:
            continue
        # Un aviso publicado hoy no habla de hace mas de dos meses
        if cand >= today - timedelta(days=60):
            return cand
    return None


def starts_later(text: str, today: date | None = None) -> date | None:
    """Devuelve la fecha de inicio si el aviso es para MAS ADELANTE.

    None significa "esto va ahora" (o no se puede saber, que se trata igual).
    """
    if not text:
        return None
    today = today or date.today()

    if _NOW_RX.search(text):
        return None

    found = []
    for m in _DATE_RX.finditer(text):
        d = _to_date(int(m.group(1)), MONTHS[m.group(2).lower()], today)
        if d:
            found.append((m.start(), d))
    if not found:
        return None

    first_pos, first_date = min(found, key=lambda x: x[1])

    # "jusqu'au X" antes de la primera fecha => ya esta en marcha
    if _UNTIL_RX.search(text[:first_pos]):
        return None

    return first_date if first_date > today else None
