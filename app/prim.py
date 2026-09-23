"""Cliente de la API PRIM con cache, control de cuota y degradacion elegante.

Decisiones importantes (medidas contra la API real, ver README):
  - La cabecera de autenticacion es 'apikey'. 'API Key' devuelve 401.
  - La base de Navitia es /marketplace/v2/navitia/, NO /v2/navitia/.
  - Cada endpoint tiene su propio contador de 1000 llamadas/dia que resetea
    a medianoche UTC. El limitante NO es el calculador de itinerarios sino
    stop-monitoring, porque es el que se refresca cada 30 s.
  - Pedir el StopArea padre devuelve las salidas de todos sus andenes hijos,
    asi que basta UNA llamada por estacion en vez de una por linea.
"""
import asyncio
import logging
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("trajet.prim")

BASE = "https://prim.iledefrance-mobilites.fr"

# Solo para tests: un httpx.MockTransport que hace de PRIM (tests/fakeprim.py).
# En produccion es None y httpx usa la red.
transport_override: httpx.AsyncBaseTransport | None = None
SIRI = f"{BASE}/marketplace"
NAVITIA = f"{BASE}/marketplace/v2/navitia"


class Entry:
    """Una respuesta cacheada. Se conserva aunque caduque, para poder
    servir el ultimo dato conocido si la API se cae.

    La entrada NO lleva su propio TTL: la frescura la decide quien pregunta.
    El tablero, con un tren saliendo en 3 min, quiere un dato de menos de
    20 s; el recolector se conforma con uno de 2 min. Si la caducidad se
    guardara con la entrada, mandaria el TTL del ultimo que la trajo, y el
    tablero podia recibir un dato de 2 min por haber pasado antes el
    recolector (o el recolector gastar una llamada que no queria).
    """

    __slots__ = ("data", "fetched_at")

    def __init__(self, data):
        self.data = data
        self.fetched_at = time.time()

    @property
    def age(self) -> float:
        return time.time() - self.fetched_at

    def fresh_for(self, ttl: float) -> bool:
        return self.age < ttl


class PrimError(Exception):
    """Fallo al hablar con PRIM.

    `kind` dice por que, para que la API pueda ensenar un estado disenado:
    no_key | invalid | forbidden | quota | unreachable | http
    """

    def __init__(self, msg: str = "", kind: str = "http"):
        super().__init__(msg)
        self.kind = kind


class PrimClient:
    def __init__(self, api_key: str):
        self._key = api_key
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Cuota restante que reporta cada endpoint en sus cabeceras
        self.quota: dict[str, int] = {}
        self.last_error: str | None = None

    async def start(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"apikey": self._key, "Accept": "application/json"},
            limits=httpx.Limits(max_connections=8),
            transport=transport_override,
        )

    async def close(self):
        if self._client:
            await self._client.aclose()

    def has_key(self) -> bool:
        return bool(self._key)

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _get(self, url: str, params, cache_key: str, ttl: int,
                   quota_bucket: str) -> tuple[Any, float]:
        """Devuelve (datos, antiguedad_en_segundos).

        Si la API falla pero tenemos una copia vieja, la devolvemos con su
        antiguedad en vez de reventar: mas vale un dato de hace 2 minutos
        que una pantalla en blanco en mitad de un anden.
        """
        hit = self._cache.get(cache_key)
        if hit and hit.fresh_for(ttl):
            return hit.data, hit.age

        async with self._lock(cache_key):
            # Otra peticion pudo refrescarlo mientras esperabamos el lock
            hit = self._cache.get(cache_key)
            if hit and hit.fresh_for(ttl):
                return hit.data, hit.age

            try:
                assert self._client is not None, "cliente no iniciado"
                r = await self._client.get(url, params=params)
                remaining = r.headers.get("x-ratelimit-remaining-day")
                if remaining is not None:
                    try:
                        self.quota[quota_bucket] = int(remaining)
                    except ValueError:
                        pass
                r.raise_for_status()
                data = r.json()
                self._cache[cache_key] = Entry(data)
                self.last_error = None
                return data, 0.0
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                self.last_error = msg
                log.warning("fallo %s -> %s", cache_key, msg)
                if hit:
                    # Servimos lo ultimo que supimos, con su edad real
                    return hit.data, hit.age
                raise PrimError(msg) from e

    # ---------------- SIRI ----------------

    async def stop_monitoring(self, siri_stop_area: str,
                              ttl: int | None = None) -> tuple[Any, float]:
        """Proximos pasos en una estacion (todos sus andenes y lineas).

        El ttl se puede alargar por estacion: si el proximo paso de esa parada
        sale dentro de 40 minutos, no tiene ningun sentido volver a pedirla
        cada 25 segundos. Ver board.station_ttl().
        """
        return await self._get(
            f"{SIRI}/stop-monitoring",
            {"MonitoringRef": siri_stop_area},
            f"sm:{siri_stop_area}",
            settings.ttl_stop_monitoring if ttl is None else ttl,
            "stop-monitoring",
        )

    async def general_message(self) -> tuple[Any, float]:
        """Todas las perturbaciones de la red en una sola llamada."""
        return await self._get(
            f"{SIRI}/general-message",
            {"LineRef": "ALL"},
            "gm:ALL",
            settings.ttl_general_message,
            "general-message",
        )

    # ---------------- Navitia ----------------

    async def places(self, q: str, kinds: tuple[str, ...] = ("stop_area",),
                     count: int = 12) -> tuple[Any, float]:
        """Busca sitios. 'kinds' permite pedir tambien direcciones postales.

        El id que devuelve Navitia para una direccion es "lon;lat", y ese
        mismo id vale luego como origen o destino de /journeys, asi que no
        hace falta geocodificar nada por nuestra cuenta.
        """
        params: list[tuple[str, str]] = [("q", q), ("count", str(count))]
        for k in kinds:
            params.append(("type[]", k))
        return await self._get(
            f"{NAVITIA}/places",
            params,
            f"pl:{'+'.join(kinds)}:{q.lower().strip()}",
            settings.ttl_places,
            "navitia",
        )

    async def line_info(self, navitia_line_id: str) -> tuple[Any, float]:
        return await self._get(
            f"{NAVITIA}/lines/{navitia_line_id}",
            {},
            f"ln:{navitia_line_id}",
            settings.ttl_line_info,
            "navitia",
        )

    async def lines_at_stop(self, navitia_stop_area: str) -> tuple[Any, float]:
        return await self._get(
            f"{NAVITIA}/stop_areas/{navitia_stop_area}/lines",
            {"count": 60},
            f"ls:{navitia_stop_area}",
            settings.ttl_line_info,
            "navitia",
        )

    async def journeys(self, frm: str, to: str, forbidden: list[str] | None = None,
                       datetime_str: str | None = None,
                       count: int = 3, represents: str = "departure"
                       ) -> tuple[Any, float]:
        """Itinerarios entre dos zonas.

        forbidden_uris[] es la clave del PASO 2: permite pedir una alternativa
        que NO pase por la linea que esta caida. httpx serializa una lista de
        tuplas repitiendo la clave, que es justo lo que espera Navitia.
        """
        params: list[tuple[str, str]] = [
            ("from", frm),
            ("to", to),
            ("min_nb_journeys", str(count)),
            ("max_nb_journeys", str(count + 2)),
            ("data_freshness", "realtime"),
        ]
        if datetime_str:
            params.append(("datetime", datetime_str))
            # "arrival" planifica hacia atras: para llegar a las 09:00, sale
            # a las 08:42. Es como se piensa un trayecto al trabajo.
            params.append(("datetime_represents", represents))
        for f in forbidden or []:
            params.append(("forbidden_uris[]", f))

        key = (f"jr:{frm}>{to}|{','.join(sorted(forbidden or []))}"
               f"|{datetime_str or ''}|{represents}|{count}")
        return await self._get(f"{NAVITIA}/journeys", params, key,
                               settings.ttl_journeys, "navitia")


client: PrimClient | None = None


def get_client() -> PrimClient:
    assert client is not None, "PrimClient no inicializado"
    return client


async def startup() -> None:
    """Crea el cliente con la clave en uso. (La FASE 1 lo completa con el
    almacen de la clave y el contador de cuota.)"""
    global client
    client = PrimClient(settings.api_key)
    await client.start()


async def shutdown() -> None:
    if client is not None:
        await client.close()


def server_state() -> dict:
    """ServerState de docs/openapi.yaml para cada tablero de la v1."""
    key = "valid" if client is not None and client.has_key() else "missing"
    return {"prim_key": key, "quota_level": "ok", "refresh_hint_s": 30,
            "degraded": False}
