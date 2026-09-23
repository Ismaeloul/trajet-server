"""Cliente de la API PRIM con cache, control de cuota y degradacion elegante.

Decisiones importantes (medidas contra la API real, ver README):
  - La cabecera de autenticacion es 'apikey'. 'API Key' devuelve 401.
  - La base de Navitia es /marketplace/v2/navitia/, NO /v2/navitia/.
  - Cada endpoint tiene su propio contador de 1000 llamadas/dia que resetea
    a medianoche UTC. El limitante NO es el calculador de itinerarios sino
    stop-monitoring, porque es el que se refresca cada 30 s.
  - Pedir el StopArea padre devuelve las salidas de todos sus andenes hijos,
    asi que basta UNA llamada por estacion en vez de una por linea.

Lo nuevo de la v2 (docs/servidor-v2.md, «prim»):
  - La clave sale del almacen (keystore.py: panel > entorno) y se puede
    cambiar en caliente: set_key() vacia la cache y reinicia la cuota sin
    reiniciar nada mas.
  - La cuota la cuenta quota.py. Si no queda, no se llama: se sirve la copia
    que haya, de cualquier edad. Con la cuota justa el TTL se alarga.
  - Errores tipados (PrimError.kind) para que la API ensene un estado
    disenado, y mensajes SIN URL ni parametros: httpx mete la URL completa
    en sus excepciones, y ahi iban textos buscados y coordenadas de casa.
  - Tras un fallo, pausa creciente por endpoint (hasta 60 s): con PRIM
    caido, cada refresco no vuelve a esperar 20 s por estacion.
  - Cache LRU con tope y locks que se liberan: antes crecian sin limite.
"""
import asyncio
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

import httpx

from . import keystore as KS
from . import logs
from . import quota as Q
from .config import settings

log = logging.getLogger("trajet.prim")

BASE = "https://prim.iledefrance-mobilites.fr"

# Solo para tests: un httpx.MockTransport que hace de PRIM (tests/fakeprim.py).
# En produccion es None y httpx usa la red.
transport_override: httpx.AsyncBaseTransport | None = None
SIRI = f"{BASE}/marketplace"
NAVITIA = f"{BASE}/marketplace/v2/navitia"

ENDPOINTS = Q.ENDPOINTS

# Entradas de cache como mucho. Lo que ocupa de verdad son unas pocas
# estaciones y los itinerarios; el resto (busquedas) son respuestas pequenas.
CACHE_MAX = 500

# Pausa tras fallos seguidos en un endpoint: 5, 10, 20, 40 y 60 s.
PAUSE_BASE = 5.0
PAUSE_MAX = 60.0

# Durante cuanto cuenta como "degradado" haber servido una copia vieja.
STALE_WINDOW = 120.0

# Llamadas minimas para comprobar una clave. Saint-Lazare siempre tiene
# salidas. Para Navitia, una busqueda de un solo resultado: /coverage, que
# seria lo natural, NO existe en PRIM (404, medido el 24/09/2026).
CHECK_STATION = "STIF:StopArea:SP:71370:"
CHECK_NAVITIA = ("places", {"q": "Saint-Lazare", "count": "1", "type[]": "stop_area"})

# Estados de la clave que una respuesta buena de PRIM corrige sola. 'forbidden'
# no: un 403 es de UNA API, y que otra responda bien no lo arregla.
_HEALS_ON_OK = ("unknown", "invalid", "unreachable", "quota_exhausted")


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

    El mensaje es corto y sin URL ni parametros ("HTTP 503", "tiempo de
    espera agotado"): acaba en el pie del tablero y en /api/health.
    """

    def __init__(self, msg: str = "", kind: str = "http", endpoint: str | None = None,
                 status: int | None = None):
        super().__init__(msg)
        self.kind = kind
        self.endpoint = endpoint
        self.status = status


def _status_error(status: int) -> tuple[str, str]:
    """(kind, mensaje) de una respuesta HTTP de error."""
    if status == 401:
        return "invalid", "clave no válida (HTTP 401)"
    if status == 403:
        return "forbidden", "sin permiso para esta API (HTTP 403)"
    if status == 429:
        return "quota", "cuota agotada (HTTP 429)"
    return "http", f"HTTP {status}"


def _exception_error(e: BaseException) -> tuple[str, str]:
    """(kind, mensaje) de una excepcion, sin repetir su texto (lleva la URL)."""
    if isinstance(e, (httpx.TimeoutException, TimeoutError)):
        return "unreachable", "tiempo de espera agotado"
    if isinstance(e, (httpx.ConnectError, ConnectionError, OSError)):
        return "unreachable", "no se puede conectar"
    if isinstance(e, httpx.TransportError):
        return "unreachable", "conexión cortada"
    if isinstance(e, ValueError):
        return "http", "respuesta no válida"
    return "http", f"error inesperado ({type(e).__name__})"


def _reached_prim(e: BaseException) -> bool:
    """La peticion llego a salir (PRIM la habra contado aunque no respondiera)."""
    return isinstance(e, (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError))


def _int_header(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class _Pause:
    __slots__ = ("fails", "until", "kind", "msg", "status")

    def __init__(self):
        self.fails = 0
        self.until = 0.0
        self.kind = "http"
        self.msg = ""
        self.status: int | None = None


class PrimClient:
    def __init__(self, api_key: str = "", quota: Q.Quota | None = None,
                 store: KS.KeyStore | None = None):
        self._key = (api_key or "").strip()
        self._client: httpx.AsyncClient | None = None
        self.store = store
        # Sin contador explicito (herramientas sueltas), uno en memoria.
        self.quota_counter = quota if quota is not None else Q.Quota(persist=False,
                                                                     key=self._key)
        self.cache_max = CACHE_MAX
        self._cache: OrderedDict[str, Entry] = OrderedDict()
        # clave de cache -> [lock, usuarios]; se borra al quedar sin usuarios.
        self._locks: dict[str, list] = {}
        self._pauses: dict[str, _Pause] = {}
        # endpoint -> (cuando, "endpoint: motivo") del ultimo fallo sin arreglar
        self._errors: dict[str, tuple[float, str]] = {}
        self._stale_at = 0.0
        # Cada cambio de clave sube la generacion: lo que llegue tarde de la
        # clave anterior no se cachea ni se cuenta en la nueva.
        self._gen = 0
        self._clock = time.monotonic

    async def start(self):
        # La clave va en cada peticion, no en las cabeceras del cliente: asi
        # cambiarla no toca un estado compartido con peticiones en vuelo.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"Accept": "application/json"},
            limits=httpx.Limits(max_connections=8),
            transport=transport_override,
        )

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    def has_key(self) -> bool:
        return bool(self._key)

    # ---------------- estado para la API ----------------

    @property
    def quota(self) -> dict[str, int]:
        """`quota` de la 0.3.0 (endpoint -> llamadas que quedan hoy), sacado
        del contador: ya no se queda con la cifra de ayer tras medianoche."""
        return self.quota_counter.as_legacy()

    @property
    def last_error(self) -> str | None:
        """Ultimo fallo de un endpoint que todavia no se ha arreglado, p. ej.
        'stop-monitoring: HTTP 503'. Una llamada buena a OTRO endpoint no lo
        borra (en la 0.3.0 si)."""
        errores = tuple(self._errors.values())     # foto: se lee desde otros hilos
        return max(errores)[1] if errores else None

    def key_state(self) -> str:
        if not self._key:
            return "missing"
        if self.store is None:
            return "unknown"
        return self.store.state()[0]

    def effective_ttl(self, ttl: float, endpoint: str) -> float:
        """TTL con la degradacion por cuota aplicada (ver quota.ttl_for)."""
        return self.quota_counter.ttl_for(ttl, endpoint)

    def paused(self, endpoint: str) -> bool:
        p = self._pauses.get(endpoint)
        return bool(p and p.until > self._clock())

    def degraded(self) -> bool:
        """Se esta sirviendo algo mas viejo de lo normal: cuota justa en los
        endpoints del tablero, un endpoint en pausa o una copia vieja hace
        poco."""
        if any(self.quota_counter.level(ep) != "ok" for ep in Q.BOARD_ENDPOINTS):
            return True
        if any(self.paused(ep) for ep in ENDPOINTS):
            return True
        return self._stale_at > 0 and self._clock() - self._stale_at < STALE_WINDOW

    # ---------------- clave en caliente ----------------

    async def set_key(self, key: str) -> None:
        """Cambia la clave sin reiniciar: cabecera, cache y contador nuevos.

        Todo va seguido y sin `await` por medio, asi que ninguna peticion ve
        la clave nueva con la cache o la cuota de la vieja.
        """
        key = (key or "").strip()
        logs.register_secret(key)
        self.quota_counter.reset_for_new_key(key)
        self._key = key
        self._gen += 1
        self._cache.clear()
        self._pauses.clear()
        self._errors.clear()
        self._stale_at = 0.0

    def clear_cache(self) -> None:
        self._cache.clear()

    # ---------------- cache y locks ----------------

    def _cache_get(self, key: str) -> Entry | None:
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
        return hit

    def _cache_put(self, key: str, entry: Entry) -> None:
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > max(1, self.cache_max):
            self._cache.popitem(last=False)

    @asynccontextmanager
    async def _locked(self, key: str):
        slot = self._locks.get(key)
        if slot is None:
            slot = self._locks[key] = [asyncio.Lock(), 0]
        slot[1] += 1
        try:
            async with slot[0]:
                yield
        finally:
            slot[1] -= 1
            if slot[1] == 0 and self._locks.get(key) is slot:
                del self._locks[key]

    # ---------------- la llamada ----------------

    def _blocked(self, bucket: str) -> PrimError | None:
        """Motivo para NO llamar ahora, o None si se puede."""
        if not self._key:
            return PrimError("sin clave de PRIM", "no_key", bucket)
        p = self._pauses.get(bucket)
        if p and p.until > self._clock():
            return PrimError(p.msg, p.kind, bucket, p.status)
        if not self.quota_counter.can_spend(bucket):
            return PrimError("cuota diaria agotada", "quota", bucket)
        return None

    def _fallback(self, hit: Entry | None, err: PrimError) -> tuple[Any, float]:
        if hit is not None:
            # Servimos lo ultimo que supimos, con su edad real
            self._stale_at = self._clock()
            return hit.data, hit.age
        raise err

    async def _get(self, url: str, params, cache_key: str, ttl: int,
                   quota_bucket: str) -> tuple[Any, float]:
        """Devuelve (datos, antiguedad_en_segundos).

        Si la API falla (o no se la puede llamar: sin clave, sin cuota, en
        pausa) pero tenemos una copia vieja, la devolvemos con su antiguedad
        en vez de reventar: mas vale un dato de hace 2 minutos que una
        pantalla en blanco en mitad de un anden.
        """
        ttl_eff = self.effective_ttl(ttl, quota_bucket)
        hit = self._cache_get(cache_key)
        if hit and hit.fresh_for(ttl_eff):
            return hit.data, hit.age
        blocked = self._blocked(quota_bucket)
        if blocked:
            return self._fallback(hit, blocked)

        async with self._locked(cache_key):
            # Otra peticion pudo refrescarlo (o fallar) mientras esperabamos
            hit = self._cache_get(cache_key)
            if hit and hit.fresh_for(ttl_eff):
                return hit.data, hit.age
            blocked = self._blocked(quota_bucket)
            if blocked:
                return self._fallback(hit, blocked)
            return await self._fetch(url, params, cache_key, quota_bucket, hit)

    async def _fetch(self, url, params, cache_key: str, bucket: str,
                     hit: Entry | None) -> tuple[Any, float]:
        gen = self._gen
        if self._client is None:          # parado (apagando) o sin arrancar
            return self._fallback(hit, PrimError("cliente de PRIM parado", "unreachable",
                                                 bucket))
        try:
            r = await self._client.get(url, params=params, headers={"apikey": self._key})
        except Exception as e:
            kind, msg = _exception_error(e)
            if _reached_prim(e):
                await self._spend(bucket, None, gen)
            return await self._failed(bucket, kind, msg, None, None, hit, gen)

        remaining = _int_header(r.headers.get("x-ratelimit-remaining-day"))
        await self._spend(bucket, remaining, gen)
        if not 200 <= r.status_code < 300:
            kind, msg = _status_error(r.status_code)
            return await self._failed(bucket, kind, msg, r.status_code, remaining, hit, gen)
        try:
            data = r.json()
        except ValueError:
            return await self._failed(bucket, "http", "respuesta no válida", r.status_code,
                                      remaining, hit, gen)

        if gen == self._gen:
            self._cache_put(cache_key, Entry(data))
            await self._succeeded(bucket)
        return data, 0.0

    async def _spend(self, bucket: str, remaining: int | None, gen: int) -> None:
        if gen != self._gen:
            return
        try:
            await asyncio.to_thread(self.quota_counter.spend, bucket, remaining)
        except Exception as e:           # la cuota nunca tumba una respuesta buena
            log.warning("no se pudo apuntar la cuota: %s", type(e).__name__)

    async def _succeeded(self, bucket: str) -> None:
        self._pauses.pop(bucket, None)
        self._errors.pop(bucket, None)
        if self.store is not None and self.key_state() in _HEALS_ON_OK:
            await self._set_state("valid", "")

    async def _set_state(self, state: str, detail: str) -> None:
        if self.store is None:
            return
        try:
            await asyncio.to_thread(self.store.set_state, state, detail)
        except Exception as e:
            log.warning("no se pudo apuntar el estado de la clave: %s", type(e).__name__)

    async def _failed(self, bucket: str, kind: str, msg: str, status: int | None,
                      remaining: int | None, hit: Entry | None, gen: int) -> tuple[Any, float]:
        err = PrimError(msg, kind, bucket, status)
        if gen != self._gen:
            # Respuesta de la clave anterior: no dice nada de la nueva.
            return self._fallback(hit, err)
        self._errors[bucket] = (time.time(), f"{bucket}: {msg}")
        # Ni URL ni clave de cache: llevan textos buscados y coordenadas.
        log.warning("PRIM %s: %s", bucket, msg)

        if kind == "invalid":
            await self._set_state("invalid", f"PRIM respondió 401 en {bucket}")
        elif kind == "forbidden" and self.key_state() in ("unknown", "valid"):
            await self._set_state("forbidden", f"sin permiso para {bucket} (HTTP 403)")
        if gen != self._gen:              # la clave cambio mientras se apuntaba
            return self._fallback(hit, err)

        if kind == "quota" and (remaining is None or remaining <= 0):
            # 429 de la cuota del dia: no se vuelve a llamar hasta medianoche UTC.
            await asyncio.to_thread(self.quota_counter.mark_exhausted, bucket)
        elif kind == "http" and status is not None and 400 <= status < 500:
            # Un 404/400 es de ESA peticion (una estacion que no existe), no
            # del endpoint: no se castiga a las demas.
            pass
        else:
            # Caido, clave rechazada o 429 de rafaga (la cabecera dice que
            # aun queda cuota hoy): pausa creciente para este endpoint.
            self._pause(bucket, kind, msg, status)
        return self._fallback(hit, err)

    def _pause(self, bucket: str, kind: str, msg: str, status: int | None) -> None:
        p = self._pauses.setdefault(bucket, _Pause())
        p.fails += 1
        p.until = self._clock() + min(PAUSE_MAX, PAUSE_BASE * 2 ** (p.fails - 1))
        p.kind, p.msg, p.status = kind, msg, status

    # ---------------- comprobar una clave ----------------

    async def validate_key(self, key: str) -> list[dict]:
        """PrimKeyCheck por API, con una llamada minima a cada una.

        Usa un cliente httpx aparte con ESA clave: la que esta en uso no se
        toca, ni su cache, ni su pausa. Tampoco se cuenta en su cuota.
        """
        checks, _ = await self._validate(key)
        return checks

    async def _validate(self, key: str) -> tuple[list[dict], dict[str, int | None]]:
        key = (key or "").strip()
        logs.register_secret(key)
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0),
                                     headers={"Accept": "application/json"},
                                     transport=transport_override) as cli:
            results = await asyncio.gather(
                _check(cli, key, "stop-monitoring", f"{SIRI}/stop-monitoring",
                       {"MonitoringRef": CHECK_STATION}),
                _check(cli, key, "general-message", f"{SIRI}/general-message",
                       {"LineRef": "ALL"}),
                _check(cli, key, "navitia", f"{NAVITIA}/{CHECK_NAVITIA[0]}",
                       CHECK_NAVITIA[1]),
            )
        checks = [c for c, _ in results]
        remaining = {c["api"]: rem for c, rem in results}
        if key and key == self._key:
            # Es la clave en uso: lo que dice PRIM de su cuota vale, aunque
            # estas llamadas no se cuenten como trafico de la app.
            for ep, rem in remaining.items():
                await asyncio.to_thread(self.quota_counter.observe, ep, rem)
        return checks, remaining

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


async def _check(cli: httpx.AsyncClient, key: str, api: str, url: str,
                 params: dict) -> tuple[dict, int | None]:
    """Una comprobacion: (PrimKeyCheck, x-ratelimit-remaining-day)."""
    def out(ok: bool, status: int | None, message: str) -> dict:
        return {"api": api, "ok": ok, "status": status, "message": message}

    if not key:
        return out(False, None, "no hay clave que comprobar"), None
    try:
        r = await cli.get(url, params=params, headers={"apikey": key})
    except httpx.TimeoutException:
        return out(False, None, "PRIM no responde (tiempo de espera agotado): prueba más tarde"), None
    except Exception:
        return out(False, None, "no se puede conectar con PRIM: revisa la conexión del servidor"), None
    st = r.status_code
    rem = _int_header(r.headers.get("x-ratelimit-remaining-day"))
    if 200 <= st < 300:
        extra = f"; quedan {rem} llamadas hoy" if rem is not None else ""
        return out(True, st, f"responde bien{extra}"), rem
    if st == 401:
        msg = "clave no válida: PRIM no la reconoce (401). Revisa que esté copiada entera"
    elif st == 403:
        msg = "la clave no tiene permiso para esta API (403): suscríbete a ella en el portal PRIM"
    elif st == 429:
        msg = "cuota diaria agotada para esta API (429): vuelve a haber a medianoche UTC"
    elif st >= 500:
        msg = f"PRIM caído o con problemas (HTTP {st}): prueba más tarde"
    else:
        msg = f"respuesta inesperada de PRIM (HTTP {st})"
    return out(False, st, msg), rem


def _verdict(checks: list[dict]) -> tuple[str, str]:
    """Estado de la clave (enum de PrimKeyInfo.state) a partir de las
    comprobaciones, con un detalle que se entiende sin mirar los logs."""
    if not checks:
        return "missing", KS.MISSING_DETAIL
    by_status: dict[int | None, list[str]] = {}
    for c in checks:
        if not c["ok"]:
            by_status.setdefault(c["status"], []).append(c["api"])
    if 401 in by_status:
        return "invalid", "PRIM no reconoce la clave (401)"
    if 403 in by_status:
        return "forbidden", f"sin permiso para: {', '.join(by_status[403])}"
    if 429 in by_status:
        return "quota_exhausted", f"cuota agotada hoy en: {', '.join(by_status[429])}"
    if by_status:
        caidas = [api for apis in by_status.values() for api in apis]
        return "unreachable", f"PRIM no responde bien en: {', '.join(caidas)}"
    return "valid", "stop-monitoring, general-message y navitia responden bien"


def _error_code(state: str) -> str:
    return {"invalid": "prim_key_rejected", "forbidden": "prim_key_rejected",
            "quota_exhausted": "prim_quota_exhausted",
            "missing": "prim_key_missing"}.get(state, "prim_unreachable")


client: PrimClient | None = None


def get_client() -> PrimClient:
    assert client is not None, "PrimClient no inicializado"
    return client


def get_keystore() -> KS.KeyStore:
    """El almacen de la clave que usa el cliente (o el de la configuracion
    actual si el cliente aun no ha arrancado)."""
    if client is not None and client.store is not None:
        return client.store
    return KS.get_store()


async def startup() -> None:
    """Crea el cliente con la clave en uso (panel > entorno) y su cuota."""
    global client
    if client is not None:
        try:
            await client.close()
        except Exception as e:     # un cliente de un bucle ya cerrado (tests)
            log.debug("no se pudo cerrar el cliente anterior: %s", type(e).__name__)
    KS.reset_store()
    Q.reset_quota()
    store = KS.get_store()
    logs.register_secret(settings.api_key)
    key, source = store.current()
    logs.register_secret(key)
    counter = Q.get_quota()
    counter.reset_for_new_key(key)
    client = PrimClient(key, quota=counter, store=store)
    await client.start()
    if not key:
        log.warning("no hay clave de PRIM: el tablero no tendra datos hasta que "
                    "se guarde una en el panel")
    log.info("clave PRIM en uso: %s", {"panel": "la del panel", "env": "la del entorno",
                                       "none": "ninguna"}[source])


async def shutdown() -> None:
    if client is not None:
        await client.close()


async def set_key_and_save(key: str) -> dict:
    """PrimKeyResult: comprueba la clave NUEVA, y solo si PRIM la acepta la
    cifra, la guarda y la pone en uso en caliente.

    Se guarda si al menos una API responde bien y ninguna la rechaza (401 o
    403). Si PRIM esta caido del todo no se puede saber si es buena, y no
    se guarda: `error` dice por que.
    """
    c = get_client()
    store = get_keystore()
    key = (key or "").strip()
    if not key:
        return {"saved": False, "info": store.info(), "checks": [],
                "error": {"code": "bad_request", "message": "la clave está vacía"}}
    logs.register_secret(key)
    checks = await c.validate_key(key)
    state, detail = _verdict(checks)
    rejected = any(ch["status"] in (401, 403) for ch in checks)
    if rejected or not any(ch["ok"] for ch in checks):
        return {"saved": False, "info": store.info(), "checks": checks,
                "error": {"code": _error_code(state), "message": detail}}
    await asyncio.to_thread(store.save, key, state, detail)
    await c.set_key(key)
    log.info("clave PRIM nueva guardada desde el panel y en uso")
    return {"saved": True, "info": store.info(), "checks": checks}


async def delete_saved_key() -> dict:
    """Borra la clave del panel y pasa en caliente a la del entorno (o a
    ninguna). Devuelve PrimKeyInfo, como DELETE /api/admin/prim-key."""
    c = get_client()
    store = get_keystore()
    await asyncio.to_thread(store.delete)
    key, source = store.current()
    await c.set_key(key)
    log.info("clave PRIM del panel borrada; en uso: %s",
             "la del entorno" if source == "env" else "ninguna")
    return store.info()


async def recheck() -> dict:
    """PrimKeyResult de volver a comprobar la clave en uso. `saved` es
    siempre False: aqui no se guarda ninguna clave nueva."""
    c = get_client()
    store = get_keystore()
    key, _ = store.current()
    if not key:
        return {"saved": False, "info": store.info(), "checks": [],
                "error": {"code": "prim_key_missing", "message": KS.MISSING_DETAIL}}
    checks, _ = await c._validate(key)
    state, detail = _verdict(checks)
    if store.current()[0] == key:        # nadie la ha cambiado mientras tanto
        await asyncio.to_thread(store.set_state, state, detail)
    out = {"saved": False, "info": store.info(), "checks": checks}
    if state != "valid":
        out["error"] = {"code": _error_code(state), "message": detail}
    return out


def server_state() -> dict:
    """ServerState de docs/openapi.yaml para cada tablero de la v1. Solo
    memoria: se llama en cada refresco."""
    if client is None:
        return {"prim_key": "missing", "quota_level": "ok", "refresh_hint_s": 30,
                "degraded": False}
    counter = client.quota_counter
    return {"prim_key": client.key_state(), "quota_level": counter.level(),
            "refresh_hint_s": counter.refresh_hint(), "degraded": client.degraded()}


def prim_state() -> dict:
    """PrimState de docs/openapi.yaml (nunca la clave)."""
    if client is None:
        return {"key_state": "missing", "key_source": "none", "checked_at": None,
                "last_error": None}
    store = get_keystore()
    _, source = store.current()
    st, _, checked = store.state()
    if not client.has_key():
        st, source = "missing", "none"
    return {"key_state": st, "key_source": source, "checked_at": checked,
            "last_error": client.last_error}


def quota_snapshot() -> dict:
    """QuotaV1 de docs/openapi.yaml."""
    counter = client.quota_counter if client is not None else Q.get_quota()
    return counter.snapshot()
