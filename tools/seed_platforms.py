"""Siembra la prevision de anden con lo que ya midio el sondeo.

El sondeo del 30/08 guardo 32.122 observaciones en Saint-Lazare y Gare du
Nord. En vez de empezar de cero, se meten en la tabla que usa la prevision.
Son pocos trenes con via (el 17 %) y de un solo dia, asi que no basta para
predecir con confianza, pero arranca el historico.

    python3 tools/seed_platforms.py probe_observations.jsonl

Es idempotente: la tabla tiene un indice unico por (dia, parada, linea, tren,
hora, destino), asi que ejecutarlo dos veces no infla las cuentas.
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, platform          # noqa: E402
from app.config import settings       # noqa: E402
from app.idfm import line_code, real_platform  # noqa: E402

# Las estaciones que sondeo, en el formato que usa la app.
STATIONS = {
    "saint-lazare": "stop_area:IDFM:71370",
    "gare-du-nord": "stop_area:IDFM:71410",
}


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def main(path):
    db.init()
    total = leidas = nuevas = 0
    sin_estacion = set()

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue

            via = real_platform(r.get("pdep")) or real_platform(r.get("parr"))
            if not via:
                continue

            stop_id = STATIONS.get(r.get("st", ""))
            if not stop_id:
                sin_estacion.add(r.get("st", ""))
                continue

            code = line_code(r.get("line") or "")
            if not code:
                continue

            # El sondeo apunta la linea en formato SIRI; la app guarda los
            # tramos en formato Navitia.
            line_id = f"line:IDFM:{code}"

            visto = parse(r.get("t"))
            aimed = parse(r.get("aimed")) or parse(r.get("exp"))
            if not visto:
                continue
            visto_local = visto.astimezone(settings.tz)
            aimed_hhmm = (aimed.astimezone(settings.tz).strftime("%H:%M")
                          if aimed else "")

            leidas += 1
            if platform.record(stop_id, line_id, r.get("dest") or "",
                               r.get("train"), aimed_hhmm, via,
                               when=visto_local):
                nuevas += 1

    print(f"filas leidas:            {total}")
    print(f"con anden de verdad:     {leidas}")
    print(f"observaciones nuevas:    {nuevas}")
    if sin_estacion:
        print(f"estaciones desconocidas: {sorted(sin_estacion)}")

    acc = platform.accuracy()
    print(f"\nen la base de datos: {acc['observations']} observaciones "
          f"de {acc['days']} dia(s)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: seed_platforms.py <probe_observations.jsonl>")
    main(sys.argv[1])
