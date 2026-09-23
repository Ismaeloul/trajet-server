"""Comprueba la promesa de "si la API cae, no ves una pantalla en blanco".

La app debe seguir enseñando el ultimo dato conocido con su antiguedad, y solo
fallar si nunca llego a tener uno. Tambien comprueba que la cache no gasta
cuota de mas y que un fallo en una estacion no tumba las demas.
"""
import asyncio
import sys
import time

sys.path.insert(0, ".")

from app import prim as P
from app.prim import Entry, PrimClient, PrimError

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'MAL'} {name}" + ("" if cond else f"   <<< {extra}"))
    if not cond:
        FAILS.append(name)


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.headers = {"x-ratelimit-remaining-day": "742"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._data


class FakeHttp:
    """Cliente falso: cuenta llamadas y puede empezar a fallar cuando queramos."""

    def __init__(self):
        self.calls = 0
        self.broken = False
        self.payload = {"v": 1}

    async def get(self, url, params=None):
        self.calls += 1
        if self.broken:
            raise ConnectionError("la red se cayo")
        return FakeResponse(self.payload)


async def main():
    client = PrimClient("clave-de-mentira")
    http = FakeHttp()
    client._client = http

    print("=== cache ===")
    d1, age1 = await client.stop_monitoring("STIF:StopArea:SP:71370:")
    check("primera llamada llega a la API", http.calls == 1, f"{http.calls} llamadas")
    check("devuelve el dato", d1 == {"v": 1})
    check("edad cero recien traido", age1 == 0.0, age1)

    d2, age2 = await client.stop_monitoring("STIF:StopArea:SP:71370:")
    check("segunda llamada sale de cache", http.calls == 1, f"{http.calls} llamadas")

    print("\n=== cuota leida de las cabeceras ===")
    check("cuota registrada", client.quota.get("stop-monitoring") == 742, client.quota)

    print("\n=== la API se cae ===")
    http.broken = True
    # Envejecemos la entrada 90 s de verdad, no solo caducandola: asi se
    # comprueba que la antiguedad que llega a la pantalla es la real.
    entry = client._cache["sm:STIF:StopArea:SP:71370:"]
    entry.fetched_at -= 90
    d3, age3 = await client.stop_monitoring("STIF:StopArea:SP:71370:")
    check("sigue devolviendo el ultimo dato conocido", d3 == {"v": 1})
    check("y con su antiguedad real (~90 s)", 89 <= age3 <= 92, f"edad {age3:.1f} s")
    check("registra el error", client.last_error is not None, client.last_error)

    print("\n=== sin dato previo y con la API caida ===")
    try:
        await client.stop_monitoring("STIF:StopArea:SP:99999:")
        check("lanza PrimError si nunca hubo dato", False, "no lanzo nada")
    except PrimError:
        check("lanza PrimError si nunca hubo dato", True)

    print("\n=== se recupera sola ===")
    http.broken = False
    http.payload = {"v": 2}
    client._cache["sm:STIF:StopArea:SP:71370:"].fetched_at -= 90
    d4, age4 = await client.stop_monitoring("STIF:StopArea:SP:71370:")
    check("vuelve el dato fresco", d4 == {"v": 2}, d4)
    check("edad vuelve a cero", age4 == 0.0, age4)
    check("limpia el error", client.last_error is None, client.last_error)

    print("\n=== peticiones simultaneas no duplican llamadas ===")
    client._cache.clear()
    before = http.calls
    await asyncio.gather(*[
        client.stop_monitoring("STIF:StopArea:SP:12345:") for _ in range(5)
    ])
    check("5 peticiones a la vez = 1 sola llamada",
          http.calls - before == 1, f"{http.calls - before} llamadas")

    print("\n=== un tramo roto no tumba el tablero ===")
    from app.board import build_board

    P.client = client
    http.broken = True
    client._cache.clear()
    route = {
        "id": 1, "name": "prueba",
        "origin_name": "A", "dest_name": "B",
        "legs": [{
            "seq": 0, "line_id": "line:IDFM:C01739", "line_code": "J",
            "line_name": "J", "line_mode": "Train", "line_color": "CEC73D",
            "from_id": "stop_area:IDFM:71370", "from_name": "Saint-Lazare",
            "to_id": "", "to_name": "", "directions": [],
        }],
    }
    board = await build_board(route)
    check("el tablero se construye igual", isinstance(board, dict) and "legs" in board)
    check("el tramo aparece aunque sin datos", len(board["legs"]) == 1)
    check("sin salidas, no revienta", board["legs"][0]["departures"] == [])
    check("y lo cuenta como error", len(board["errors"]) > 0, board["errors"])

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLOS: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
