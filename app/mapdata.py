"""Datos del mapa: trazados, paradas, accesos y transbordos con los datos
abiertos de IDFM (docs/datos-idfm.md §8 y esquema RouteMap de docs/openapi.yaml).

Principios (docs/datos-idfm.md §8.1):
  - Solo lo que usan las rutas guardadas, pedido por linea y por zona. Nunca
    un volcado entero: el dataset de trazados pesa unos 100 MB y parseado no
    cabe en los 384 MB del contenedor. Toda peticion al portal lleva `where`.
  - Nunca en el camino critico del tablero: se calcula en segundo plano, se
    guarda ya procesado en SQLite (`map_cache`) y se sirve de ahi.
  - Lo ultimo bueno se conserva siempre: una fila solo se sustituye por otra
    ya validada. Si el portal cae se sirve lo guardado con `stale: true`.
  - Cliente HTTP propio SIN la cabecera `apikey` (R90): el de PRIM la pone en
    todas sus peticiones y reutilizarlo mandaria la clave a Opendatasoft.

Uso desde la API:
  await route_map(db.get_route(id, with_coords=True))   -> RouteMap
  schedule_route(id)          al crear o editar una ruta
  status()                    para el panel (hace un COUNT: con run_in_threadpool)
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import heapq
import json
import logging
import math
import re
import struct
import time
import zlib
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

import httpx
from fastapi.concurrency import run_in_threadpool

from . import db
from .config import VERSION, settings
from .idfm import line_code, norm_text, sa_code

log = logging.getLogger("trajet.mapdata")

# Solo para tests: un transporte httpx que hace de portal (tests/_mapa_portal.py).
# En produccion es None y httpx usa la red.
transport_override: httpx.AsyncBaseTransport | None = None

# ---------------- datasets ----------------

DS_LINES = "referentiel-des-lignes"
DS_STOPS = "arrets-lignes"
DS_TRACES = "traces-des-lignes-de-transport-en-commun-idfm"
DS_FERRE = "traces-du-reseau-ferre-idf"
DS_GARES = "emplacement-des-gares-idf"
DS_ZDC = "zones-de-correspondance"
DS_ZDA = "zones-d-arrets"
DS_ARRETS = "arrets"
DS_ART = "arrets-transporteur"
DS_REL_ACC = "relations-acces"
DS_ACC = "acces"
DS_GTFS = "offre-horaires-tc-gtfs-idfm"

CATALOG_DATASETS = (DS_LINES, DS_STOPS, DS_TRACES, DS_FERRE, DS_GARES, DS_ZDC,
                    DS_ZDA, DS_ARRETS, DS_ART, DS_REL_ACC, DS_ACC)
_LICENCE_OUVERTE = (DS_FERRE, DS_GARES, DS_ZDC, DS_ZDA, DS_ARRETS, DS_ART,
                    DS_REL_ACC, DS_ACC)

# Dias minimos entre dos descargas del mismo dato aunque el catalogo diga que
# ha cambiado (§8.7). Los trazados GTFS cambian `data_processed` tres veces al
# dia sin que la geometria cambie; el ferrocarril y las estaciones son
# «Annuelle» en origen. El resto (referencial de paradas, accesos): a diario.
_MIN_DAYS = {DS_TRACES: 7, DS_STOPS: 7, DS_FERRE: 30, DS_GARES: 30}
_GTFS_CHECK_DAYS = 7

GTFS_URL_DEFAULT = "https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip"

# ---------------- red ----------------

USER_AGENT = f"Trajet/{VERSION} (servidor casero; mapa con datos abiertos de IDFM)"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRY_WAITS = (2.0, 6.0)        # dos reintentos con espera creciente
BREAKER_FAILS = 3               # fallos seguidos que abren la pausa
BREAKER_PAUSE = 30 * 60         # segundos sin volver a intentarlo
# Tope de lo que se acepta en una respuesta. La linea mas larga medida son
# ~1,1 MB de GeoJSON y parseado ocupa unas 8 veces mas: con 3 MB el pico se
# queda en la frontera de los 20 MB del presupuesto (§8.8).
MAX_BYTES = 3_000_000
_ZIP_TAIL = 65_557              # EOCD (22) + comentario maximo del zip (65 535)

# ---------------- geometria ----------------

_R = 6_371_008.8
_KX = math.radians(1.0) * math.cos(math.radians(48.85)) * _R   # m por grado de lon
_KY = math.radians(1.0) * _R                                    # m por grado de lat
NEAR_STOP_M = 50.0              # parada a esta distancia del trazado = pasa por ella
NEAR_VIA_M = 30.0               # parada intermedia
JOIN_M = 60.0                   # extremos de tramos ferroviarios que se unen
TOL_COARSE = 20.0
TOL_FINE = 2.0
# Ile-de-France con margen: la J llega a Gisors y a Vernon, fuera de la region.
_BBOX = (1.3, 47.9, 3.7, 49.5)  # lon_min, lat_min, lon_max, lat_max

STARTUP_DELAY = 60              # s: no competir con el arranque del servidor
REFRESH_AT = (3, 30)            # hora de Paris de la comprobacion diaria

# El texto de docs/datos-idfm.md §5.4 (la fecha sale de `data_processed`).
_LICENSE = ("Datos: Île-de-France Mobilités — Référentiel des arrêts, accès et "
            "tracés du réseau ferré (Licence Ouverte v2.0{fecha}) ; tracés des "
            "lignes, référentiel des lignes, arrêts et lignes associées (ODbL) ; "
            "horaires GTFS (Licence Mobilités). Tracés calculés sur OpenStreetMap "
            "© contributeurs OpenStreetMap.")


class PortalError(Exception):
    """Fallo al hablar con el portal. `kind`: down (red, 5xx, 429, pausa) |
    http (4xx: la peticion esta mal, reintentar no arregla nada) | data."""

    def __init__(self, msg: str, kind: str = "down"):
        super().__init__(msg)
        self.kind = kind


class _Transient(Exception):
    pass


# =====================================================================
# Geometria (Python puro: proyeccion plana local, DP, polilinea, Lambert)
# =====================================================================

def _xy(lon: float, lat: float) -> tuple[float, float]:
    """Proyeccion plana local en metros. Con la latitud fija de Paris el error
    en Ile-de-France es despreciable (docs/datos-idfm.md §8.3)."""
    return lon * _KX, lat * _KY


def _cumulative(xys: list[tuple[float, float]]) -> list[float]:
    cum = [0.0]
    for (ax, ay), (bx, by) in zip(xys, xys[1:]):
        cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
    return cum


def path_length(lonlat: list) -> float:
    """Longitud en metros de una lista de [lon, lat]."""
    xys = [_xy(p[0], p[1]) for p in lonlat]
    return _cumulative(xys)[-1] if xys else 0.0


def _near_runs(p: tuple[float, float], xys: list, cum: list, max_d: float) -> list[tuple[float, float]]:
    """Pasadas del recorrido junto al punto: (distancia, posicion) de la
    proyeccion mas cercana de cada tramo seguido de segmentos a <= max_d.

    No basta con la proyeccion mas cercana de todo el recorrido: una linea
    que pasa dos veces cerca (un bucle, ida y vuelta solapadas) tiene que dar
    las dos pasadas para poder elegir la buena.
    """
    px, py = p
    runs: list[tuple[float, float]] = []
    best: tuple[float, float] | None = None
    for i in range(len(xys) - 1):
        ax, ay = xys[i]
        bx, by = xys[i + 1]
        # Descarte rapido por caja: la mayoria de segmentos estan lejos.
        if (px < min(ax, bx) - max_d or px > max(ax, bx) + max_d
                or py < min(ay, by) - max_d or py > max(ay, by) + max_d):
            if best is not None:
                runs.append(best)
                best = None
            continue
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d <= max_d:
            along = cum[i] + t * math.sqrt(l2)
            if best is None or d < best[0]:
                best = (d, along)
        elif best is not None:
            runs.append(best)
            best = None
    if best is not None:
        runs.append(best)
    return runs


def _nearest(p: tuple[float, float], xys: list, cum: list) -> tuple[float, float, int]:
    """(distancia, posicion, segmento) de la proyeccion mas cercana."""
    px, py = p
    best = (math.inf, 0.0, 0)
    for i in range(len(xys) - 1):
        ax, ay = xys[i]
        bx, by = xys[i + 1]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
        d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if d < best[0]:
            best = (d, cum[i] + t * math.sqrt(l2), i)
    return best


def _point_at(lonlat: list, cum: list, s: float) -> tuple[float, float]:
    i = max(0, min(len(cum) - 2, bisect_right(cum, s) - 1))
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg <= 0 else (s - cum[i]) / seg
    (ax, ay), (bx, by) = lonlat[i], lonlat[i + 1]
    return (ax + (bx - ax) * t, ay + (by - ay) * t)


def cut_path(lonlat: list, cum: list, a: float, b: float) -> list[tuple[float, float]]:
    """El trozo del recorrido entre las posiciones a < b (en metros),
    interpolando los extremos."""
    out = [_point_at(lonlat, cum, a)]
    for i in range(len(lonlat)):
        if a < cum[i] < b:
            out.append((lonlat[i][0], lonlat[i][1]))
    out.append(_point_at(lonlat, cum, b))
    return out


def douglas_peucker(lonlat: list, tol_m: float) -> list[tuple[float, float]]:
    """Douglas-Peucker con la tolerancia en metros (sobre la proyeccion plana).

    Iterativo: un recorrido de 28 000 puntos haria reventar la recursion.
    Distancia al SEGMENTO, no a la recta: con la recta, un trazado que vuelve
    sobre si mismo perderia la punta.
    """
    n = len(lonlat)
    if n <= 2:
        return [(p[0], p[1]) for p in lonlat]
    xys = [_xy(p[0], p[1]) for p in lonlat]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = xys[a]
        bx, by = xys[b]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        dmax, imax = -1.0, -1
        for i in range(a + 1, b):
            px, py = xys[i]
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > dmax:
                dmax, imax = d, i
        if dmax > tol_m:
            keep[imax] = True
            stack.append((a, imax))
            stack.append((imax, b))
    return [(lonlat[i][0], lonlat[i][1]) for i in range(n) if keep[i]]


def encode_polyline(lonlat: list, precision: int = 5) -> str:
    """Polilinea codificada de Google (lat, lon). Precision 5 = ~1,1 m."""
    factor = 10 ** precision
    out: list[str] = []
    plat = plon = 0
    for lon, lat in lonlat:
        ilat = math.floor(lat * factor + 0.5)
        ilon = math.floor(lon * factor + 0.5)
        if out and ilat == plat and ilon == plon:
            continue                      # dos puntos que redondean igual
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def decode_polyline(text: str, precision: int = 5) -> list[tuple[float, float]]:
    """Inversa de encode_polyline. Devuelve [(lat, lon)]."""
    factor = 10 ** precision
    out: list[tuple[float, float]] = []
    i = lat = lon = 0
    n = len(text)
    while i < n:
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                if i >= n:
                    raise ValueError("polilinea cortada")
                b = ord(text[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append((lat / factor, lon / factor))
    return out


def lambert93_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Lambert 93 (EPSG:2154) -> (lon, lat) WGS84. Algoritmo IGN ALG0004.

    Hace falta porque `zones-de-correspondance` solo trae el centroide de la
    zona en Lambert 93 (sin geopoint). RGF93 y WGS84 difieren en centimetros.
    """
    n = 0.7256077650532670
    c = 11754255.426096
    xs, ys = 700000.0, 12655612.049876
    e = 0.0818191910428158
    lon0 = math.radians(3.0)
    dx, dy = x - xs, y - ys
    r = math.hypot(dx, dy)
    gamma = math.atan(dx / -dy)
    lon = lon0 + gamma / n
    lat_iso = -1.0 / n * math.log(abs(r / c))
    phi = 2.0 * math.atan(math.exp(lat_iso)) - math.pi / 2.0
    for _ in range(30):
        es = e * math.sin(phi)
        nxt = 2.0 * math.atan(((1 + es) / (1 - es)) ** (e / 2.0) * math.exp(lat_iso)) - math.pi / 2.0
        if abs(nxt - phi) < 1e-12:
            phi = nxt
            break
        phi = nxt
    return math.degrees(lon), math.degrees(phi)


def _in_idf(lon, lat) -> bool:
    try:
        return _BBOX[0] <= float(lon) <= _BBOX[2] and _BBOX[1] <= float(lat) <= _BBOX[3]
    except (TypeError, ValueError):
        return False


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    (ax, ay), (bx, by) = _xy(*a), _xy(*b)
    return math.hypot(bx - ax, by - ay)


# =====================================================================
# Cliente del portal (httpx propio, sin apikey) y lector del zip GTFS
# =====================================================================

async def _sin_apikey(request: httpx.Request) -> None:
    """Ultima barrera de R90: aunque alguien la anadiera, no sale."""
    if "apikey" in request.headers:
        del request.headers["apikey"]


class _Resp:
    __slots__ = ("status", "headers", "content")

    def __init__(self, status: int, headers: httpx.Headers, content: bytes):
        self.status = status
        self.headers = headers
        self.content = content


class _Portal:
    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._fails: dict[str, int] = {}
        self._paused_until: dict[str, float] = {}
        self.requests = 0

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Cabeceras propias y nada mas: ni apikey ni nada heredado de PRIM.
            self._client = httpx.AsyncClient(
                timeout=TIMEOUT,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
                limits=httpx.Limits(max_connections=4),
                transport=transport_override,
                follow_redirects=True,
                event_hooks={"request": [_sin_apikey]},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def paused(self, host: str) -> bool:
        return self._paused_until.get(host, 0.0) > time.monotonic()

    def _failed(self, host: str) -> bool:
        self._fails[host] = self._fails.get(host, 0) + 1
        if self._fails[host] >= BREAKER_FAILS:
            self._paused_until[host] = time.monotonic() + BREAKER_PAUSE
            log.warning("portal %s: %d fallos seguidos, pausa de %d min",
                        host, self._fails[host], BREAKER_PAUSE // 60)
            return True
        return False

    async def request(self, url: str, *, params: dict | None = None,
                      headers: dict | None = None, expect: tuple = (200,),
                      max_bytes: int = MAX_BYTES,
                      on_chunk: Callable[[bytes], Awaitable[None]] | None = None) -> _Resp:
        """GET con reintentos, pausa tras fallos seguidos y tope de tamano.

        Con `on_chunk` el cuerpo va por trozos a esa funcion y no se guarda.
        Una vez entregado un trozo ya no se reintenta (quien lo recibe tendria
        el principio repetido): el fallo se cuenta y se lanza.
        """
        host = httpx.URL(url).host
        if self.paused(host):
            raise PortalError(f"{host}: en pausa tras {BREAKER_FAILS} fallos seguidos", "down")
        last: Exception | None = None
        for attempt in range(1 + len(RETRY_WAITS)):
            if attempt:
                await asyncio.sleep(RETRY_WAITS[attempt - 1])
            fed = False
            try:
                async with self.client().stream("GET", url, params=params, headers=headers) as r:
                    self.requests += 1
                    st = r.status_code
                    if st == 304 and 304 in expect:
                        self._fails[host] = 0
                        return _Resp(304, r.headers, b"")
                    if st >= 500 or st == 429:
                        raise _Transient(f"HTTP {st}")
                    if st not in expect:
                        # El portal responde: esta vivo. La peticion esta mal
                        # (o el servidor no admite Range): no se reintenta y,
                        # sobre todo, no se lee un cuerpo que no se ha pedido.
                        self._fails[host] = 0
                        raise PortalError(f"{host}: HTTP {st}", "http")
                    body = bytearray()
                    n = 0
                    async for chunk in r.aiter_bytes():
                        n += len(chunk)
                        if n > max_bytes:
                            raise PortalError(f"{host}: respuesta de mas de {max_bytes} bytes", "data")
                        if on_chunk is None:
                            body += chunk
                        else:
                            fed = True
                            await on_chunk(chunk)
                self._fails[host] = 0
                return _Resp(st, r.headers, bytes(body))
            except (httpx.TransportError, httpx.DecodingError, _Transient) as e:
                last = e
                if self._failed(host) or fed:
                    break
        raise PortalError(f"{host}: {type(last).__name__}: {last}", "down")

    # ---------------- Opendatasoft ----------------

    def _base(self) -> str:
        return f"{settings.idfm_portal}/api/explore/v2.1/catalog/datasets"

    async def export(self, dataset: str, fmt: str, where: str,
                     select: str | None = None) -> tuple[Any, str]:
        """Exportacion FILTRADA de un dataset: (datos, sha256 del crudo)."""
        if not where:
            raise ValueError("nada de volcados completos: toda exportacion lleva where")
        params = {"where": where}
        if select:
            params["select"] = select
        r = await self.request(f"{self._base()}/{dataset}/exports/{fmt}", params=params)
        digest = hashlib.sha256(r.content).hexdigest()
        try:
            data = await run_in_threadpool(json.loads, r.content)
        except ValueError as e:
            raise PortalError(f"{dataset}: JSON no valido ({e})", "data") from e
        if fmt == "geojson":
            if not isinstance(data, dict) or not isinstance(data.get("features"), list):
                raise PortalError(f"{dataset}: GeoJSON sin features", "data")
        elif not isinstance(data, list):
            raise PortalError(f"{dataset}: se esperaba una lista", "data")
        return data, digest

    async def catalog(self, datasets) -> dict[str, str]:
        """Frescura de los datasets en UNA llamada (§8.7): {id: data_processed}."""
        where = _in("dataset_id", datasets)
        r = await self.request(self._base(), params={
            "where": where, "select": "dataset_id,data_processed", "limit": 100})
        try:
            data = await run_in_threadpool(json.loads, r.content)
            return {x["dataset_id"]: x["data_processed"] for x in data.get("results", [])
                    if x.get("dataset_id") and x.get("data_processed")}
        except (ValueError, AttributeError, TypeError) as e:
            raise PortalError(f"catalogo no valido ({e})", "data") from e


_portal = _Portal()


def _gtfs_url() -> str:
    return getattr(settings, "idfm_gtfs_url", "") or GTFS_URL_DEFAULT


class _GtfsZip:
    """Indice del zip GTFS remoto, leido con dos peticiones Range pequenas."""

    def __init__(self, etag: str, last_modified: str, members: dict):
        self.etag = etag
        self.last_modified = last_modified
        self.members = members        # nombre -> (metodo, comprimido, tamano, offset)

    def version(self) -> str:
        return self.etag or self.last_modified

    def if_range(self) -> dict:
        v = self.etag or self.last_modified
        return {"If-Range": v} if v else {}


def _zip_eocd(tail: bytes, tail_start: int) -> tuple[int, int]:
    """(offset, tamano) del directorio central, con zip64 si hace falta."""
    i = tail.rfind(b"PK\x05\x06")
    if i < 0 or len(tail) < i + 22:
        raise ValueError("sin fin de directorio central")
    _, _, _, _, total, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", tail[i:i + 22])
    if cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF or total == 0xFFFF:
        j = tail.rfind(b"PK\x06\x07", 0, i)
        if j < 0:
            raise ValueError("zip64 sin localizador")
        _, _, z64, _ = struct.unpack("<IIQI", tail[j:j + 20])
        k = z64 - tail_start
        if k < 0 or tail[k:k + 4] != b"PK\x06\x06":
            raise ValueError("zip64: registro fuera del final leido")
        cd_size, cd_off = struct.unpack("<QQ", tail[k + 40:k + 56])
    return cd_off, cd_size


def _zip_members(cd: bytes, wanted: set[str]) -> dict:
    out = {}
    pos = 0
    while pos + 46 <= len(cd) and cd[pos:pos + 4] == b"PK\x01\x02":
        (_, _, _, _, method, _, _, _, csize, usize, nlen, elen, clen,
         _, _, _, off) = struct.unpack("<IHHHHHHIIIHHHHHII", cd[pos:pos + 46])
        name = cd[pos + 46:pos + 46 + nlen].decode("utf-8", "replace")
        if 0xFFFFFFFF in (csize, usize, off):
            extra = cd[pos + 46 + nlen:pos + 46 + nlen + elen]
            p = 0
            while p + 4 <= len(extra):
                hid, hlen = struct.unpack("<HH", extra[p:p + 4])
                if hid == 0x0001:
                    vals = list(struct.unpack(f"<{hlen // 8}Q", extra[p + 4:p + 4 + (hlen // 8) * 8]))
                    if usize == 0xFFFFFFFF and vals:
                        usize = vals.pop(0)
                    if csize == 0xFFFFFFFF and vals:
                        csize = vals.pop(0)
                    if off == 0xFFFFFFFF and vals:
                        off = vals.pop(0)
                    break
                p += 4 + hlen
        if name in wanted:
            out[name] = (method, csize, usize, off)
        pos += 46 + nlen + elen + clen
    return out


async def _gtfs_open(if_none_match: str | None = None) -> _GtfsZip | None:
    """Lee el final del zip (EOCD + directorio central). None si no ha cambiado.

    Nunca se baja el zip entero (132 MB): si el servidor no respetara el Range
    y contestara 200, `request` corta sin leer el cuerpo.
    """
    url = _gtfs_url()
    headers = {"Range": f"bytes=-{_ZIP_TAIL}"}
    if if_none_match:
        headers["If-None-Match"] = if_none_match
    r = await _portal.request(url, headers=headers, expect=(206, 304), max_bytes=_ZIP_TAIL)
    if r.status == 304:
        return None
    m = re.match(r"bytes (\d+)-(\d+)/(\d+)", r.headers.get("content-range", ""))
    if not m:
        raise PortalError("GTFS: respuesta sin Content-Range", "data")
    start = int(m.group(1))
    etag = r.headers.get("etag", "")
    zipf = _GtfsZip(etag, r.headers.get("last-modified", ""), {})
    try:
        cd_off, cd_size = _zip_eocd(r.content, start)
        if cd_off >= start:
            cd = r.content[cd_off - start:cd_off - start + cd_size]
        else:
            r2 = await _portal.request(url, headers={
                "Range": f"bytes={cd_off}-{cd_off + cd_size - 1}", **zipf.if_range()},
                expect=(206,), max_bytes=cd_size)
            cd = r2.content
        zipf.members = _zip_members(cd, {"pathways.txt", "transfers.txt"})
    except (ValueError, struct.error) as e:
        raise PortalError(f"GTFS: zip no valido ({e})", "data") from e
    return zipf


class _CsvFilter:
    """Descomprime un miembro del zip por trozos y se queda solo con las filas
    que interesan: memoria constante aunque `transfers.txt` tenga 191 622
    filas (5,5 MB). Nunca se carga el fichero entero."""

    def __init__(self, method: int, needles: tuple[str, ...], keep: Callable[[dict], None]):
        if method not in (0, 8):
            raise PortalError(f"GTFS: compresion {method} no soportada", "data")
        self._z = zlib.decompressobj(-15) if method == 8 else None
        self._buf = b""
        self._header: list[str] | None = None
        self._needles = needles
        self._keep = keep

    def feed(self, chunk: bytes) -> None:
        self._lines(self._z.decompress(chunk) if self._z else chunk)

    def close(self) -> None:
        if self._z:
            self._lines(self._z.flush())
        if self._buf:
            self._rows([self._buf.decode("utf-8", "replace")])
            self._buf = b""

    def _lines(self, data: bytes) -> None:
        self._buf += data
        cut = self._buf.rfind(b"\n")
        if cut < 0:
            return
        block, self._buf = self._buf[:cut], self._buf[cut + 1:]
        self._rows(block.decode("utf-8", "replace").split("\n"))

    def _rows(self, lines: list[str]) -> None:
        if self._header is None and lines:
            self._header = next(csv.reader([lines[0].lstrip("\ufeff").rstrip("\r")]))
            lines = lines[1:]
        wanted = [ln.rstrip("\r") for ln in lines if any(n in ln for n in self._needles)]
        for row in csv.reader(wanted):
            self._keep(dict(zip(self._header, row)))


async def _gtfs_read(zipf: _GtfsZip, name: str, filt: _CsvFilter) -> None:
    if name not in zipf.members:
        raise PortalError(f"GTFS: falta {name}", "data")
    method, csize, _, off = zipf.members[name]
    url = _gtfs_url()
    # La cabecera local dice cuanto ocupan su nombre y su extra (pueden no
    # coincidir con los del directorio central).
    hdr = await _portal.request(url, headers={"Range": f"bytes={off}-{off + 29}", **zipf.if_range()},
                                expect=(206,), max_bytes=30)
    if len(hdr.content) < 30 or hdr.content[:4] != b"PK\x03\x04":
        raise PortalError(f"GTFS: cabecera de {name} no valida", "data")
    nlen, elen = struct.unpack("<HH", hdr.content[26:30])
    start = off + 30 + nlen + elen
    if csize > 0:
        async def feed(chunk: bytes) -> None:
            await run_in_threadpool(filt.feed, chunk)
        await _portal.request(url, headers={"Range": f"bytes={start}-{start + csize - 1}", **zipf.if_range()},
                              expect=(206,), max_bytes=csize, on_chunk=feed)
    await run_in_threadpool(filt.close)


# =====================================================================
# Persistencia (map_cache): siempre fuera del bucle de eventos
# =====================================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_days(iso: str | None) -> float:
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return math.inf
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0


def _pack(body) -> bytes:
    return gzip.compress(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), mtime=0)


def _unpack(blob) -> Any:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


def _db_get(keys: list[str]) -> dict[str, dict]:
    keys = list(dict.fromkeys(keys))
    out: dict[str, dict] = {}
    with db.conn() as c:
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            for row in c.execute(
                    "SELECT key, body, raw_hash, fetched_at, source_version FROM map_cache "
                    f"WHERE key IN ({','.join('?' * len(part))})", part):
                try:
                    body = _unpack(row["body"])
                except (OSError, ValueError, EOFError):
                    continue                  # fila rota: como si no estuviera
                out[row["key"]] = {"body": body, "raw_hash": row["raw_hash"],
                                   "fetched_at": row["fetched_at"],
                                   "source_version": row["source_version"]}
    return out


def _db_get_prefix(prefix: str) -> dict[str, dict]:
    with db.conn() as c:
        keys = [r["key"] for r in c.execute(
            "SELECT key FROM map_cache WHERE key LIKE ?", (prefix + "%",))]
    return _db_get(keys) if keys else {}


def _db_put(items: list[tuple[str, Any, str, str]]) -> None:
    if not items:
        return
    now = _now_iso()
    with db.conn() as c:
        c.executemany(
            "INSERT INTO map_cache (key, body, raw_hash, fetched_at, source_version) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET body = excluded.body, "
            "raw_hash = excluded.raw_hash, fetched_at = excluded.fetched_at, "
            "source_version = excluded.source_version",
            [(k, _pack(b), h or "", now, v or "") for k, b, h, v in items])


def _db_prune_routes(valid_ids: list[int]) -> int:
    """Quita el mapa de las rutas borradas (no toca lo compartido)."""
    keep = {f"route:{i}" for i in valid_ids}
    with db.conn() as c:
        keys = [r["key"] for r in c.execute(
            "SELECT key FROM map_cache WHERE key >= 'route:' AND key < 'route;'")]
        gone = [k for k in keys if k not in keep]
        c.executemany("DELETE FROM map_cache WHERE key = ?", [(k,) for k in gone])
    return len(gone)


def _db_count() -> int:
    with db.conn() as c:
        return int(c.execute(
            "SELECT COUNT(*) FROM map_cache WHERE key NOT LIKE 'meta:%'").fetchone()[0])


# =====================================================================
# Lectura de los datasets (ya descargados) a estructuras pequenas
# =====================================================================

_SAFE = re.compile(r"^[A-Za-z0-9:._\- ]+$")


def _in(field: str, values) -> str:
    """Clausula ODSQL `campo in ('a','b')` con valores comprobados: vienen de
    la BD, pero no se mete en una consulta nada que no sea un identificador."""
    vals = sorted({str(v) for v in values if v is not None and _SAFE.match(str(v))})
    if not vals:
        raise ValueError(f"sin valores validos para {field}")
    quoted = ", ".join(f"'{v}'" for v in vals)
    return f"{field} in ({quoted})"


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _point_of(feature: dict) -> tuple[float, float] | None:
    g = (feature or {}).get("geometry") or {}
    c = g.get("coordinates") if g.get("type") == "Point" else None
    if isinstance(c, list) and len(c) >= 2:
        lon, lat = _f(c[0]), _f(c[1])
        if lon is not None and lat is not None:
            return lon, lat
    return None


def _build_stations(zdc_raw: list, zda_raw: list, arr_raw: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for z in zdc_raw:
        zid = str(z.get("zdcid") or "")
        if not zid:
            continue
        lon = lat = None
        x, y = _f(z.get("zdcxepsg2154")), _f(z.get("zdcyepsg2154"))
        if x is not None and y is not None and y > 0:
            lon, lat = lambert93_to_wgs84(x, y)
        out[zid] = {"zdc": zid, "name": z.get("zdcname") or "", "lat": lat, "lon": lon,
                    "type": z.get("zdctype") or "", "zdas": {}, "arrets": {}}
    zda_to_zdc = {}
    for a in zda_raw:
        zid, aid = str(a.get("zdcid") or ""), str(a.get("zdaid") or "")
        if zid in out and aid:
            out[zid]["zdas"][aid] = {"name": a.get("zdaname") or "", "type": a.get("zdatype") or ""}
            zda_to_zdc[aid] = zid
    for f in arr_raw.get("features", []):
        p = f.get("properties") or {}
        aid, rid = str(p.get("zdaid") or ""), str(p.get("arrid") or "")
        pt = _point_of(f)
        if aid in zda_to_zdc and rid and pt and _in_idf(*pt):
            out[zda_to_zdc[aid]]["arrets"][rid] = {
                "name": p.get("arrname") or "", "type": p.get("arrtype") or "",
                "zda": aid, "lon": round(pt[0], 6), "lat": round(pt[1], 6)}
    for st in out.values():
        if st["lat"] is None and st["arrets"]:
            # Sin centroide: el centro de sus paradas.
            pts = list(st["arrets"].values())
            st["lon"] = sum(p["lon"] for p in pts) / len(pts)
            st["lat"] = sum(p["lat"] for p in pts) / len(pts)
        if st["lat"] is not None:
            st["lon"], st["lat"] = round(st["lon"], 6), round(st["lat"], 6)
    return {k: v for k, v in out.items() if v["lat"] is not None and _in_idf(v["lon"], v["lat"])}


def _parse_stops(raw: list) -> list[dict]:
    out = []
    seen = set()
    for s in raw:
        sid = s.get("stop_id")
        lon, lat = _f(s.get("stop_lon")), _f(s.get("stop_lat"))
        if not sid or sid in seen or lon is None or lat is None or not _in_idf(lon, lat):
            continue
        seen.add(sid)
        out.append({"id": sid, "name": s.get("stop_name") or "", "lon": lon, "lat": lat})
    return out


def _parse_parts(raw: dict) -> list[list[tuple[float, float]]]:
    parts = []
    for f in raw.get("features", []):
        g = f.get("geometry") or {}
        lines = [g.get("coordinates")] if g.get("type") == "LineString" else (
            g.get("coordinates") if g.get("type") == "MultiLineString" else [])
        for line in lines or []:
            pts = [(float(p[0]), float(p[1])) for p in line or [] if isinstance(p, list) and len(p) >= 2]
            if len(pts) >= 2:
                parts.append(pts)
    return parts


def _parse_ferre(raw: dict, code: str) -> list[tuple[int, list]]:
    """Tramos de la linea. Se filtra por `idrefligc` y TAMBIEN por `res_com`:
    el tramo 550 de la J trae `idrefligc='C0173'` (truncado) y solo se
    reconoce por su `res_com='TRAIN J'`. Y no se fia de `indice_lig`: el '14'
    mezcla el metro 14 y el tranvia T14."""
    feats = raw.get("features", [])
    res = {(f.get("properties") or {}).get("res_com") for f in feats
           if (f.get("properties") or {}).get("idrefligc") == code} - {None, ""}
    out = []
    for f in feats:
        p = f.get("properties") or {}
        if p.get("idrefligc") != code and p.get("res_com") not in res:
            continue
        g = f.get("geometry") or {}
        lines = [g.get("coordinates")] if g.get("type") == "LineString" else (
            g.get("coordinates") if g.get("type") == "MultiLineString" else [])
        for line in lines or []:
            pts = [(float(q[0]), float(q[1])) for q in line or [] if isinstance(q, list) and len(q) >= 2]
            if len(pts) >= 2:
                out.append((int(p.get("objectid_1") or 0), pts))
    return out


def _parse_gares(raw: dict, code: str, res: set) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for f in raw.get("features", []):
        p = f.get("properties") or {}
        if p.get("idrefligc") != code and p.get("res_com") not in res:
            continue
        pt = _point_of(f)
        z = str(p.get("id_ref_zdc") or "")
        if pt and z and z not in out:
            out[z] = pt
    return out


def _build_accesses(rel_raw: list, acc_raw: dict, zdas: list[str]) -> dict[str, list[dict]]:
    by_acc: dict[str, dict] = {}
    for f in acc_raw.get("features", []):
        p = f.get("properties") or {}
        aid = str(p.get("accid") or "")
        pt = _point_of(f)
        if not aid or not pt or not _in_idf(*pt):
            continue
        num = p.get("accshortname")
        if isinstance(num, float) and num.is_integer():
            num = int(num)
        by_acc[aid] = {
            "id": aid, "name": p.get("accname") or "",
            "number": str(num) if num not in (None, "") else None,
            "entry": str(p.get("accisentry")).lower() == "true",
            "exit": str(p.get("accisexit")).lower() == "true",
            "lat": round(pt[1], 6), "lon": round(pt[0], 6)}
    out: dict[str, list[dict]] = {z: [] for z in zdas}
    for r in rel_raw:
        z, a = str(r.get("zdaid") or ""), str(r.get("accid") or "")
        if z in out and a in by_acc and all(x["id"] != a for x in out[z]):
            out[z].append(by_acc[a])
    return out


def _build_tracks(art_raw: list, arrid_to_zda: dict, zdas: list[str]) -> dict[str, list[dict]]:
    out: dict[str, dict[str, dict]] = {z: {} for z in zdas}
    for r in art_raw:
        z = arrid_to_zda.get(str(r.get("arrid") or ""))
        voie = str(r.get("publiccode") or "").strip()
        g = r.get("artgeopoint") or {}
        lon, lat = _f(g.get("lon")), _f(g.get("lat"))
        # Solo vias de verdad: el arret generico de la estacion trae '-'.
        if z is None or not voie or voie == "-" or lon is None or not _in_idf(lon, lat):
            continue
        if str(r.get("fournisseurname") or "SNCF").upper() != "SNCF":
            continue
        out[z].setdefault(voie, {"voie": voie, "lat": round(lat, 6), "lon": round(lon, 6)})

    def orden(t):
        return (0, int(t["voie"]), "") if t["voie"].isdigit() else (1, 0, t["voie"])
    return {z: sorted(v.values(), key=orden) for z, v in out.items()}


# =====================================================================
# Calculo de un tramo (§8.3): recorte GTFS, plan B ferroviario, recta
# =====================================================================

def _stop_ref(stop_id: str) -> tuple[str, str]:
    """IDFM:monomodalStopPlace:58566 -> ('zda', '58566'); IDFM:462972 -> ('arr', '462972')."""
    s = str(stop_id or "")
    if ":monomodalStopPlace:" in s:
        return "zda", s.rsplit(":", 1)[-1]
    return "arr", s.rsplit(":", 1)[-1]


def _stop_zda(stop_id: str | None, station: dict | None) -> str | None:
    if not stop_id or not station:
        return None
    kind, ident = _stop_ref(stop_id)
    if kind == "zda":
        return ident if ident in station["zdas"] else None
    arr = station["arrets"].get(ident)
    return arr["zda"] if arr else None


def _candidates(stops: list[dict], station: dict | None, fallback: dict | None) -> list[dict]:
    """Paradas de la linea que caen en la zona (en un bus suelen ser dos
    postes, uno por sentido). Sin ninguna, el punto de reserva."""
    out = []
    if station:
        for s in stops:
            kind, ident = _stop_ref(s["id"])
            if (kind == "zda" and ident in station["zdas"]) or (kind == "arr" and ident in station["arrets"]):
                out.append(s)
    if not out and fallback:
        out.append(fallback)
    return out


def _plan_gtfs(parts: list, cands_from: list, cands_to: list) -> dict | None:
    """El recorte mas corto de las partes que pasan por las dos paradas (a
    <= 50 m) y en el orden bueno. Las partes son un recorrido por sentido y
    variante, solapados: el mas corto descarta las variantes largas."""
    found = []
    pf = [_xy(c["lon"], c["lat"]) for c in cands_from]
    pt = [_xy(c["lon"], c["lat"]) for c in cands_to]
    for part in parts:
        xys = [_xy(p[0], p[1]) for p in part]
        cum = _cumulative(xys)
        runs_f = [_near_runs(p, xys, cum, NEAR_STOP_M) for p in pf]
        if not any(runs_f):
            continue
        runs_t = [_near_runs(p, xys, cum, NEAR_STOP_M) for p in pt]
        for i, rf in enumerate(runs_f):
            for j, rt in enumerate(runs_t):
                for df, af in rf:
                    for dt, at in rt:
                        if at - af >= 1.0:
                            found.append((at - af, df + dt, i, j, af, at, part, cum))
    if not found:
        return None
    # Variantes: gana la mas corta. Entre recortes casi iguales (los dos
    # postes de una parada de bus distan unos metros) gana el de las paradas
    # mas pegadas al trazado, que es el poste por el que pasa ese sentido.
    shortest = min(f[0] for f in found)
    close = [f for f in found if f[0] <= shortest + 2 * NEAR_STOP_M]
    _, _, i, j, af, at, part, cum = min(close, key=lambda f: (f[1], f[0]))
    return {"coords": cut_path(part, cum, af, at), "from": cands_from[i], "to": cands_to[j],
            "source": "gtfs"}


def _plan_ferre(tramos: list, p_from: tuple[float, float], p_to: tuple[float, float]) -> dict | None:
    """Grafo de tramos ferroviarios (extremos a < 60 m = mismo nodo) y
    Dijkstra por longitud entre las dos estaciones (`heapq`)."""
    nodes: list[tuple[float, float]] = []

    def node_for(xy):
        for i, n in enumerate(nodes):
            if math.hypot(n[0] - xy[0], n[1] - xy[1]) < JOIN_M:
                return i
        nodes.append(xy)
        return len(nodes) - 1

    edges: list[list] = []          # [u, v, coords, xys, cum, objectid]
    for oid, coords in tramos:
        xys = [_xy(p[0], p[1]) for p in coords]
        edges.append([node_for(xys[0]), node_for(xys[-1]), coords, xys, _cumulative(xys), oid])

    def attach(lonlat):
        """Nodo de la estacion: el extremo cercano o un corte del tramo."""
        p = _xy(*lonlat)
        best = None
        for k, e in enumerate(edges):
            d, along, seg = _nearest(p, e[3], e[4])
            if best is None or d < best[0]:
                best = (d, along, seg, k)
        if best is None or best[0] > JOIN_M:
            return None
        d, along, seg, k = best
        u, v, coords, xys, cum, oid = edges[k]
        if along < JOIN_M:
            return u
        if cum[-1] - along < JOIN_M:
            return v
        q = _point_at(coords, cum, along)
        n = len(nodes)
        nodes.append(_xy(*q))
        c1 = coords[:seg + 1] + [q]
        c2 = [q] + coords[seg + 1:]
        for idx, (a, b, cc) in enumerate(((u, n, c1), (n, v, c2))):
            xx = [_xy(pp[0], pp[1]) for pp in cc]
            e = [a, b, cc, xx, _cumulative(xx), oid]
            if idx == 0:
                edges[k] = e
            else:
                edges.append(e)
        return n

    s = attach(p_from)
    t = attach(p_to)
    if s is None or t is None or s == t:
        return None
    adj: dict[int, list[tuple[int, int]]] = {}
    for k, e in enumerate(edges):
        adj.setdefault(e[0], []).append((e[1], k))
        adj.setdefault(e[1], []).append((e[0], k))
    dist = {s: 0.0}
    prev: dict[int, tuple[int, int]] = {}
    heap = [(0.0, s)]
    while heap:
        d, u = heapq.heappop(heap)
        if u == t:
            break
        if d > dist.get(u, math.inf):
            continue
        for v, k in adj.get(u, []):
            nd = d + edges[k][4][-1]
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = (u, k)
                heapq.heappush(heap, (nd, v))
    if t not in dist:
        return None
    chain = []
    v = t
    while v != s:
        u, k = prev[v]
        chain.append((u, v, k))
        v = u
    chain.reverse()
    coords: list[tuple[float, float]] = []
    oids: list[int] = []
    for u, _v, k in chain:
        e = edges[k]
        seg = e[2] if e[0] == u else list(reversed(e[2]))
        coords.extend(seg if not coords else seg[1:])
        if not oids or oids[-1] != e[5]:
            oids.append(e[5])
    return {"coords": coords, "tramos": oids, "length": dist[t]}


def _via(coords: list, stops: list[dict], exclude: set, ends: tuple[str, str]) -> list[dict]:
    """Paradas de la linea a <= 30 m del recorte, en orden, sin las de los
    extremos y sin repetir nombre (un bus tiene un poste por sentido)."""
    xys = [_xy(p[0], p[1]) for p in coords]
    cum = _cumulative(xys)
    total = cum[-1] if cum else 0.0
    ends_n = {norm_text(x) for x in ends if x}
    found = []
    for s in stops:
        if s["id"] in exclude or norm_text(s["name"]) in ends_n:
            continue
        runs = _near_runs(_xy(s["lon"], s["lat"]), xys, cum, NEAR_VIA_M)
        if not runs:
            continue
        d, along = min(runs)
        if along <= NEAR_VIA_M or along >= total - NEAR_VIA_M:
            continue
        found.append((along, d, s))
    found.sort(key=lambda x: (x[0], x[1]))
    out, seen = [], set()
    for _, _, s in found:
        k = norm_text(s["name"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"name": s["name"], "lat": round(s["lat"], 6), "lon": round(s["lon"], 6)})
    return out


def _encode_path(coords: list) -> dict:
    return {
        "encoding": "polyline5",
        "coarse": encode_polyline(douglas_peucker(coords, TOL_COARSE)),
        "fine": encode_polyline(douglas_peucker(coords, TOL_FINE)),
        "tolerance_m": {"coarse": TOL_COARSE, "fine": TOL_FINE},
    }


def _stop_out(c: dict) -> dict:
    return {"stop_id": c.get("id"), "name": c.get("name") or "",
            "lat": round(c["lat"], 6), "lon": round(c["lon"], 6)}


def _platforms(cands: list[dict], station: dict | None) -> list[dict]:
    """Andenes o postes (nivel ArR) de la linea en la zona: los que el tablero
    nombra `STIF:StopPoint:Q:<arrid>:`. Las paradas de nivel ZdA (trenes SNCF)
    no son andenes: sus vias van aparte."""
    out = []
    for c in cands:
        if not c.get("id"):
            continue
        kind, ident = _stop_ref(c["id"])
        if kind != "arr":
            continue
        arr = (station or {}).get("arrets", {}).get(ident) or {}
        out.append({"arrid": ident, "name": arr.get("name") or c.get("name") or None,
                    "lat": arr.get("lat", round(c["lat"], 6)), "lon": arr.get("lon", round(c["lon"], 6))})
    return out


def _solve_leg(leg: dict, stops: list[dict], parts: list | None, st_from: dict | None,
               st_to: dict | None, ferre: dict | None = None) -> dict:
    """Todo el calculo de CPU de un tramo. Devuelve el cuerpo del tramo, o
    {"need_ferre": True} si el recorte GTFS no sale, el modo no es bus y aun
    no se ha descargado el ferrocarril."""
    cf = _candidates(stops, st_from, _fallback(leg, "from", st_from))
    ct = _candidates(stops, st_to, _fallback(leg, "to", st_to))
    if not cf or not ct:
        return {"skip": True}
    plan = _plan_gtfs(parts, cf, ct) if parts else None
    tramos = None
    if plan is None and leg["mode"] != "bus":
        if ferre is None:
            return {"need_ferre": True}
        pf = ferre["gares"].get(leg["zfrom"]) or (cf[0]["lon"], cf[0]["lat"])
        pt = ferre["gares"].get(leg["zto"]) or (ct[0]["lon"], ct[0]["lat"])
        fb = _plan_ferre(ferre["tramos"], pf, pt) if ferre["tramos"] else None
        if fb is not None:
            plan = {"coords": fb["coords"], "from": cf[0], "to": ct[0], "source": "ferre"}
            tramos = fb["tramos"]
    if plan is None:
        # Plan C: recta entre las dos paradas mas cercanas entre si.
        a, b = min(((a, b) for a in cf for b in ct),
                   key=lambda ab: _dist((ab[0]["lon"], ab[0]["lat"]), (ab[1]["lon"], ab[1]["lat"])))
        plan = {"coords": [(a["lon"], a["lat"]), (b["lon"], b["lat"])], "from": a, "to": b, "source": "recta"}
    coords = plan["coords"]
    length = path_length(coords)
    via = [] if plan["source"] == "recta" else _via(
        coords, stops, {plan["from"].get("id"), plan["to"].get("id")},
        (plan["from"].get("name"), plan["to"].get("name")))
    body = {
        "from": _stop_out(plan["from"]), "to": _stop_out(plan["to"]),
        "path": _encode_path(coords) if length >= 1.0 else None,
        "length_m": int(round(length)) if length >= 1.0 else None,
        "via": via, "source": plan["source"] if length >= 1.0 else "none",
        "platforms": {"from": _platforms(cf, st_from), "to": _platforms(ct, st_to)},
        "points": len(coords),
    }
    if tramos:
        body["tramos"] = tramos
    return body


def _fallback(leg: dict, side: str, station: dict | None) -> dict | None:
    """Punto de reserva de un extremo: las coordenadas guardadas del tramo
    (Navitia) y, si no, el centroide de la zona."""
    lat, lon = leg.get(f"{side}_lat"), leg.get(f"{side}_lon")
    name = leg.get(f"{side}_name") or (station or {}).get("name") or ""
    if lat is not None and lon is not None and _in_idf(lon, lat):
        return {"id": None, "name": name, "lat": float(lat), "lon": float(lon)}
    if station:
        return {"id": None, "name": station.get("name") or name, "lat": station["lat"], "lon": station["lon"]}
    return None


def _recta(leg: dict, st_from: dict | None, st_to: dict | None) -> dict | None:
    """Tramo sin datos del portal: recta entre los puntos que se conozcan."""
    a, b = _fallback(leg, "from", st_from), _fallback(leg, "to", st_to)
    if not a or not b:
        return None
    coords = [(a["lon"], a["lat"]), (b["lon"], b["lat"])]
    length = path_length(coords)
    ok = length >= 1.0
    return {"from": _stop_out(a), "to": _stop_out(b),
            "path": _encode_path(coords) if ok else None,
            "length_m": int(round(length)) if ok else None, "via": [],
            "source": "recta" if ok else "none", "platforms": {"from": [], "to": []}}


def _valid_leg(body: dict) -> bool:
    """Nada se guarda sin validar: al menos un punto y todo en Ile-de-France."""
    try:
        for side in ("from", "to"):
            if not _in_idf(body[side]["lon"], body[side]["lat"]):
                return False
        if body.get("path"):
            for key in ("coarse", "fine"):
                pts = decode_polyline(body["path"][key])
                if len(pts) < 1 or not all(_in_idf(lon, lat) for lat, lon in pts):
                    return False
        return all(_in_idf(v["lon"], v["lat"]) for v in body.get("via", []))
    except (KeyError, TypeError, ValueError):
        return False


# =====================================================================
# Calculo de una ruta
# =====================================================================

def _hex(v) -> str | None:
    s = str(v or "").strip().lstrip("#")
    return "#" + s.upper() if re.fullmatch(r"[0-9A-Fa-f]{6}", s) else None


def _contrast(color: str) -> str:
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    return "#000000" if (r * 299 + g * 587 + b * 114) / 1000 >= 128 else "#FFFFFF"


def _mode(ref: dict | None, leg: dict) -> str:
    m = str((ref or {}).get("transportmode") or "").lower()
    if m in ("rail", "metro", "tram", "bus"):
        return m
    if m:
        return "other"               # funicular, teleferico, barco...
    t = norm_text(leg.get("line_mode") or "")
    if "metro" in t:
        return "metro"
    if "tram" in t:
        return "tram"
    if "bus" in t:
        return "bus"
    if any(k in t for k in ("rer", "train", "transilien", "ter")):
        return "rail"
    return "other"


def _legs_of(route: dict) -> list[dict]:
    """Tramos de la ruta con lo que el mapa necesita. Un tramo de la 0.3.0
    puede no tener `to_id`: se deduce del siguiente tramo o del destino."""
    legs = sorted(route.get("legs") or [], key=lambda x: x.get("seq", 0))
    out = []
    for i, leg in enumerate(legs):
        to_id = leg.get("to_id") or (legs[i + 1].get("from_id") if i + 1 < len(legs)
                                     else route.get("dest_id")) or ""
        code = line_code(leg.get("line_id") or "")
        out.append({
            "seq": int(leg.get("seq", i)), "line_id": leg.get("line_id") or "",
            "code": code if re.fullmatch(r"[A-Za-z0-9]+", code or "") else "",
            "zfrom": _zdc(leg.get("from_id")), "zto": _zdc(to_id),
            "from_name": leg.get("from_name") or "", "to_name": leg.get("to_name") or "",
            "from_lat": _f(leg.get("from_lat")), "from_lon": _f(leg.get("from_lon")),
            "to_lat": _f(leg.get("to_lat")), "to_lon": _f(leg.get("to_lon")),
            "line_code": leg.get("line_code") or "", "line_mode": leg.get("line_mode") or "",
            "line_color": leg.get("line_color") or "",
        })
    return out


def _zdc(value) -> str:
    z = sa_code(str(value or ""))
    return z if z.isdigit() else ""


def fingerprint(route: dict) -> str:
    """Lo que define el mapa de una ruta: sus lineas y sus zonas."""
    legs = [(x["line_id"], x["zfrom"], x["zto"]) for x in _legs_of(route)]
    return hashlib.sha256(json.dumps(legs).encode()).hexdigest()[:32]


class _Ctx:
    """Un calculo: modo, errores del portal y escrituras pendientes."""

    def __init__(self, mode: str, catalog: dict | None = None):
        self.mode = mode                 # cache | compute | refresh
        self.catalog = catalog
        self.errors: list[str] = []
        self.writes: list[tuple[str, Any, str, str]] = []
        self.sources: dict[str, str] = {}
        self.gtfs_src: dict | None = None

    @property
    def online(self) -> bool:
        return self.mode != "cache"

    def fail(self, e: Exception) -> None:
        msg = str(e) or type(e).__name__
        if msg not in self.errors:
            self.errors.append(msg)
            log.warning("mapa: %s", msg)

    def put(self, key: str, body, raw_hash: str = "", version: str = "") -> None:
        self.writes.append((key, body, raw_hash, version))

    def note(self, version: str) -> None:
        """Apunta las fechas de los datos que entran en el mapa (`sources`)."""
        try:
            for ds, dp in json.loads(version or "{}").items():
                if dp and dp > self.sources.get(ds, ""):
                    self.sources[ds] = dp
        except (ValueError, AttributeError):
            pass

    def version(self, datasets) -> str:
        cat = self.catalog or {}
        return json.dumps({d: cat[d] for d in sorted(set(datasets)) if d in cat},
                          separators=(",", ":"))

    def should_fetch(self, row: dict | None, datasets) -> bool:
        if row is None:
            return self.online
        if self.mode != "refresh" or not self.catalog:
            return False
        try:
            stored = json.loads(row.get("source_version") or "{}")
        except ValueError:
            stored = {}
        changed = any(self.catalog.get(d) and self.catalog.get(d) != stored.get(d) for d in datasets)
        if not changed:
            return False
        return _age_days(row.get("fetched_at")) >= max(_MIN_DAYS.get(d, 0) for d in datasets)


async def _rows(keys) -> dict[str, dict]:
    keys = list(keys)
    return await run_in_threadpool(_db_get, keys) if keys else {}


async def _load_catalog(ctx: _Ctx) -> None:
    """Fechas de los datasets: las guardadas y, si hace falta, el catalogo."""
    rows = await run_in_threadpool(_db_get_prefix, "src:")
    ctx.gtfs_src = (rows.get("src:gtfs") or {}).get("body")
    if ctx.catalog is not None:
        return
    cat = {k[4:]: (r["body"] or {}).get("data_processed") for k, r in rows.items() if k != "src:gtfs"}
    cat = {k: v for k, v in cat.items() if v}
    fresh = any(_age_days(r["fetched_at"]) < 1 for k, r in rows.items() if k != "src:gtfs")
    if ctx.online and (ctx.mode == "refresh" or not fresh):
        try:
            new = await _portal.catalog(CATALOG_DATASETS)
            cat.update(new)
            ctx.writes.extend((f"src:{d}", {"data_processed": dp}, "", "") for d, dp in new.items())
        except PortalError as e:
            ctx.fail(e)
    ctx.catalog = cat


async def _lines(ctx: _Ctx, codes: list[str]) -> dict[str, dict]:
    keys = {c: f"line:{c}" for c in codes if c}
    rows = await _rows(keys.values())
    out = {c: rows[k]["body"] for c, k in keys.items() if k in rows}
    need = [c for c, k in keys.items() if ctx.should_fetch(rows.get(k), (DS_LINES,))]
    for c, k in keys.items():
        if k in rows and c not in need:
            ctx.note(rows[k]["source_version"])
    if need:
        try:
            raw, _ = await _portal.export(DS_LINES, "json", _in("id_line", need),
                                          "id_line,name_line,shortname_line,transportmode,"
                                          "transportsubmode,networkname,colourweb_hexa,"
                                          "textcolourweb_hexa,status")
            got = {str(x.get("id_line")): x for x in raw if isinstance(x, dict)}
            version = ctx.version((DS_LINES,))
            for c in need:
                x = got.get(c)
                if not x:
                    continue
                body = {"code": c, "short": x.get("shortname_line") or x.get("name_line") or "",
                        "transportmode": x.get("transportmode") or "",
                        "color": _hex(x.get("colourweb_hexa")),
                        "text_color": _hex(x.get("textcolourweb_hexa"))}
                out[c] = body
                ctx.put(keys[c], body, "", version)
                ctx.note(version)
        except (PortalError, ValueError) as e:
            ctx.fail(e)
            for c in need:
                if keys[c] in rows:
                    ctx.note(rows[keys[c]]["source_version"])
    return out


async def _stations(ctx: _Ctx, zdcs: list[str]) -> tuple[dict[str, dict], dict[str, str]]:
    """(zona -> {name, lat, lon, zdas, arrets}, zona -> hash del dato)."""
    datasets = (DS_ZDC, DS_ZDA, DS_ARRETS)
    keys = {z: f"zdc:{z}" for z in zdcs if z}
    rows = await _rows(keys.values())
    out = {z: rows[k]["body"] for z, k in keys.items() if k in rows}
    hashes = {z: rows[k]["raw_hash"] for z, k in keys.items() if k in rows}
    need = [z for z, k in keys.items() if ctx.should_fetch(rows.get(k), datasets)]
    for k in keys.values():
        if k in rows:
            ctx.note(rows[k]["source_version"])
    if need:
        try:
            zdc_raw, _ = await _portal.export(DS_ZDC, "json", _in("zdcid", need),
                                              "zdcid,zdcname,zdctype,zdcxepsg2154,zdcyepsg2154")
            zda_raw, _ = await _portal.export(DS_ZDA, "json", _in("zdcid", need),
                                              "zdaid,zdaname,zdatype,zdcid")
            zda_ids = [str(a.get("zdaid")) for a in zda_raw if str(a.get("zdcid")) in need and a.get("zdaid")]
            arr_raw = {"features": []}
            if zda_ids:
                arr_raw, _ = await _portal.export(DS_ARRETS, "geojson", _in("zdaid", zda_ids),
                                                  "arrid,arrname,arrtype,zdaid,arrgeopoint")
            built = await run_in_threadpool(_build_stations, zdc_raw, zda_raw, arr_raw)
            version = ctx.version(datasets)
            for z in need:
                body = built.get(z)
                if body is None:
                    continue
                h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                out[z], hashes[z] = body, h
                ctx.put(keys[z], body, h, version)
                ctx.note(version)
        except (PortalError, ValueError) as e:
            ctx.fail(e)
    return out, hashes


async def _leg(ctx: _Ctx, leg: dict, stations: dict, hashes: dict) -> dict | None:
    """Cuerpo del tramo: el guardado si vale, o uno nuevo calculado y validado.

    Si el portal falla: lo guardado (el calculo acaba `stale`) o, sin nada
    guardado, la recta entre los puntos que se conozcan (que no se guarda).
    """
    st_from, st_to = stations.get(leg["zfrom"]), stations.get(leg["zto"])
    if not (leg["code"] and leg["zfrom"] and leg["zto"]):
        return None if ctx.mode == "cache" else _recta(leg, st_from, st_to)
    key = f"leg:{leg['code']}:{leg['zfrom']}:{leg['zto']}"
    row = (await _rows([key])).get(key)
    datasets = tuple((row or {}).get("body", {}).get("datasets") or (DS_TRACES, DS_STOPS))
    if not ctx.should_fetch(row, datasets):
        if row is None:
            return None                          # modo cache y nada guardado
        ctx.note(row["source_version"])
        return row["body"]

    code = leg["code"]
    try:
        stops_raw, hs = await _portal.export(DS_STOPS, "json", f"id='IDFM:{code}'",
                                             "stop_id,stop_name,stop_lon,stop_lat,mode")
        trace_raw, ht = await _portal.export(DS_TRACES, "geojson", f"id_ilico='{code}'",
                                             "id_ilico,route_type,route_color,shape")
    except PortalError as e:
        ctx.fail(e)
        if row is not None:
            ctx.note(row["source_version"])
            return row["body"]
        return _recta(leg, st_from, st_to)

    # Con los mismos datos crudos no se recalcula (§8.6): solo se renueva la fecha.
    raw_hash = hashlib.sha256("|".join([
        hs, ht, hashes.get(leg["zfrom"], ""), hashes.get(leg["zto"], ""),
        str(leg["from_lat"]), str(leg["from_lon"]), str(leg["to_lat"]), str(leg["to_lon"]),
    ]).encode()).hexdigest()
    if row is not None and row["raw_hash"] == raw_hash:
        version = ctx.version(datasets)
        ctx.put(key, row["body"], raw_hash, version)
        ctx.note(version)
        return row["body"]

    stops = await run_in_threadpool(_parse_stops, stops_raw)
    parts = await run_in_threadpool(_parse_parts, trace_raw)
    del stops_raw, trace_raw                     # el GeoJSON crudo ya no hace falta
    body = await run_in_threadpool(_solve_leg, leg, stops, parts, st_from, st_to)
    used = [DS_TRACES, DS_STOPS]
    # Solo se guarda lo calculado con todos sus datos: con una estacion que no
    # se pudo leer, el recorte sale de puntos de reserva y no es el bueno.
    complete = st_from is not None and st_to is not None
    if body.get("need_ferre"):
        ferre = await _ferre(ctx, leg)
        if ferre is None:
            complete = False
            ferre = {"tramos": [], "gares": {}}
        else:
            used += [DS_FERRE, DS_GARES]
        body = await run_in_threadpool(_solve_leg, leg, stops, parts, st_from, st_to, ferre)
    if body.get("skip"):
        return None
    body["datasets"] = used
    version = ctx.version(used)
    ctx.note(version)
    if complete and _valid_leg(body):
        ctx.put(key, body, raw_hash, version)
    elif complete:
        log.warning("mapa: tramo %s no valido, no se guarda", key)
    return body


async def _ferre(ctx: _Ctx, leg: dict) -> dict | None:
    """Tramos ferroviarios y estaciones de la linea (plan B). Se pide por
    `idrefligc` o por el indice de la linea y se afina despues por `res_com`."""
    code = leg["code"]
    where = f"idrefligc='{code}'"
    short = leg.get("short") or ""
    if short and _SAFE.match(short):
        where += f" or indice_lig='{short}'"
    try:
        ferre_raw, _ = await _portal.export(DS_FERRE, "geojson", where,
                                            "objectid_1,idrefligc,res_com,indice_lig,mode")
        gares_raw, _ = await _portal.export(DS_GARES, "geojson", where,
                                            "nom_gares,nom_iv,id_ref_zdc,id_ref_zda,idrefligc,res_com")
    except PortalError as e:
        ctx.fail(e)
        return None

    def parse():
        tramos = _parse_ferre(ferre_raw, code)
        res = {(f.get("properties") or {}).get("res_com") for f in ferre_raw.get("features", [])
               if (f.get("properties") or {}).get("idrefligc") == code} - {None, ""}
        return {"tramos": tramos, "gares": _parse_gares(gares_raw, code, res)}
    return await run_in_threadpool(parse)


async def _accesses(ctx: _Ctx, zdas: list[str]) -> dict[str, list[dict]]:
    datasets = (DS_REL_ACC, DS_ACC)
    keys = {z: f"acc:{z}" for z in zdas}
    rows = await _rows(keys.values())
    out = {z: rows[k]["body"] for z, k in keys.items() if k in rows}
    need = [z for z, k in keys.items() if ctx.should_fetch(rows.get(k), datasets)]
    for k in keys.values():
        if k in rows:
            ctx.note(rows[k]["source_version"])
    if need:
        try:
            rel, _ = await _portal.export(DS_REL_ACC, "json", _in("zdaid", need), "zdaid,accid")
            accids = [str(r.get("accid")) for r in rel if r.get("accid")]
            acc = {"features": []}
            if accids:
                acc, _ = await _portal.export(DS_ACC, "geojson", _in("accid", accids),
                                              "accid,accname,accshortname,accisentry,accisexit,accgeopoint")
            built = await run_in_threadpool(_build_accesses, rel, acc, need)
            version = ctx.version(datasets)
            for z in need:
                out[z] = built[z]
                ctx.put(keys[z], built[z], "", version)
                ctx.note(version)
        except (PortalError, ValueError) as e:
            ctx.fail(e)
    return out


async def _tracks(ctx: _Ctx, rail: dict[str, list[str]]) -> dict[str, list[dict]]:
    """Vias SNCF de las ZdA de tren: {zda: [{voie, lat, lon}]}. `rail` es
    zda -> arrids de tipo rail de esa zona."""
    keys = {z: f"tracks:{z}" for z in rail}
    rows = await _rows(keys.values())
    out = {z: rows[k]["body"] for z, k in keys.items() if k in rows}
    need = [z for z, k in keys.items() if ctx.should_fetch(rows.get(k), (DS_ART,))]
    for k in keys.values():
        if k in rows:
            ctx.note(rows[k]["source_version"])
    arrids = {a: z for z in need for a in rail[z]}
    if need and arrids:
        try:
            raw, _ = await _portal.export(DS_ART, "json", _in("arrid", arrids),
                                          "arrid,publiccode,fournisseurname,artgeopoint")
            built = await run_in_threadpool(_build_tracks, raw, arrids, need)
            version = ctx.version((DS_ART,))
            for z in need:
                out[z] = built[z]
                ctx.put(keys[z], built[z], "", version)
                ctx.note(version)
        except (PortalError, ValueError) as e:
            ctx.fail(e)
    return out


async def _gtfs(ctx: _Ctx, stops: set[str], pairs: set[tuple[str, str]]
                ) -> tuple[dict[str, list], dict[tuple[str, str], int | None]]:
    """pathways por parada y min_transfer_time por par, del zip GTFS por Range.

    Solo se leen `pathways.txt` (75 KB comprimido) y, si la ruta tiene
    transbordos, `transfers.txt` (1,66 MB), filtrados en streaming. Se guarda
    con el ETag del zip: cuando cambia (comprobacion semanal con
    If-None-Match, que responde 304), se vuelve a leer.
    """
    pkeys = {s: f"path:{s}" for s in stops}
    xkeys = {p: f"xfer:{p[0]}>{p[1]}" for p in pairs}
    rows = await _rows(list(pkeys.values()) + list(xkeys.values()))
    src = dict(ctx.gtfs_src or {})
    version = src.get("etag") or src.get("last_modified") or ""

    def current(row):
        return row is not None and version and row["source_version"] == version
    paths = {s: rows[k]["body"] for s, k in pkeys.items() if k in rows}
    xfers = {p: rows[k]["body"].get("s") for p, k in xkeys.items() if k in rows}
    if not ctx.online:
        _note_gtfs(ctx, src, bool(paths or xfers))
        return paths, xfers
    need_s = {s for s, k in pkeys.items() if not current(rows.get(k))}
    need_p = {p for p, k in xkeys.items() if not current(rows.get(k))}
    zipf = None
    try:
        if ctx.mode == "refresh" and version and _age_days(src.get("checked_at")) >= _GTFS_CHECK_DAYS:
            zipf = await _gtfs_open(if_none_match=src.get("etag") or None)
            src["checked_at"] = _now_iso()
            if zipf is not None and zipf.version() != version:
                need_s, need_p = set(stops), set(pairs)
            ctx.put("src:gtfs", src, "", "")
        if need_s or need_p:
            if zipf is None:
                zipf = await _gtfs_open()
            if zipf.version() != version:
                # Otra version del zip: todo lo de esta ruta se relee de ella.
                need_s, need_p = set(stops), set(pairs)
            if need_s:
                found: dict[str, list] = {s: [] for s in need_s}

                def keep_path(r: dict) -> None:
                    a, b = r.get("from_stop_id", ""), r.get("to_stop_id", "")
                    m, secs = _f(r.get("length")), _f(r.get("traversal_time"))
                    bi = r.get("is_bidirectional") == "1"
                    if b in found and "StopPlaceEntrance" in a:
                        found[b].append([a, m, None if secs is None else int(secs), "both" if bi else "in"])
                    elif a in found and "StopPlaceEntrance" in b:
                        found[a].append([b, m, None if secs is None else int(secs), "both" if bi else "out"])
                await _gtfs_read(zipf, "pathways.txt",
                                 _CsvFilter(zipf.members.get("pathways.txt", (8,))[0], tuple(need_s), keep_path))
                for s in need_s:
                    paths[s] = found[s]
                    ctx.put(pkeys[s], found[s], "", zipf.version())
            if need_p:
                got: dict[tuple[str, str], int | None] = {p: None for p in need_p}

                def keep_xfer(r: dict) -> None:
                    p = (r.get("from_stop_id", ""), r.get("to_stop_id", ""))
                    if p in got:
                        t = _f(r.get("min_transfer_time"))
                        got[p] = None if t is None else int(t)
                await _gtfs_read(zipf, "transfers.txt",
                                 _CsvFilter(zipf.members.get("transfers.txt", (8,))[0],
                                            tuple({p[0] for p in need_p}), keep_xfer))
                for p in need_p:
                    xfers[p] = got[p]
                    ctx.put(xkeys[p], {"s": got[p]}, "", zipf.version())
            src.update({"etag": zipf.etag, "last_modified": zipf.last_modified,
                        "checked_at": _now_iso()})
            ctx.put("src:gtfs", src, "", "")
    except PortalError as e:
        ctx.fail(e)
    _note_gtfs(ctx, src, bool(paths or xfers))
    return paths, xfers


def _note_gtfs(ctx: _Ctx, src: dict, used: bool) -> None:
    lm = (src or {}).get("last_modified")
    if not used or not lm:
        return
    try:
        ctx.sources[DS_GTFS] = parsedate_to_datetime(lm).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        pass


def _license(sources: dict) -> str:
    fechas = [sources[d] for d in _LICENCE_OUVERTE if sources.get(d)]
    fecha = ""
    if fechas:
        try:
            t = datetime.fromisoformat(max(fechas))
            fecha = ", mise à jour du " + t.astimezone(settings.tz).strftime("%d/%m/%Y")
        except ValueError:
            fecha = ""
    return _LICENSE.format(fecha=fecha)


def _assemble(route: dict, legs: list[dict], refs: dict, stations: dict, bodies: dict,
              acc: dict, tracks: dict, paths: dict, xfers: dict, sources: dict,
              stale: bool, pending: bool) -> dict:
    """Monta el RouteMap del contrato con lo que haya."""
    lines = []
    for leg in legs:
        ref = refs.get(leg["code"])
        body = bodies.get(leg["seq"])
        st_f, st_t = stations.get(leg["zfrom"]), stations.get(leg["zto"])
        if body:
            f, t = body["from"], body["to"]
        else:
            a, b = _fallback(leg, "from", st_f), _fallback(leg, "to", st_t)
            if not a or not b:
                continue            # sin ningun punto no hay nada que pintar
            f, t = _stop_out(a), _stop_out(b)
        color = (ref or {}).get("color") or _hex(leg["line_color"]) or "#808080"
        text = ((ref or {}).get("text_color") if (ref or {}).get("color") else None) or _contrast(color)
        lines.append({
            "seq": leg["seq"], "line_id": leg["line_id"],
            "code": (ref or {}).get("short") or leg["line_code"] or leg["code"],
            "mode": _mode(ref, leg), "color": color, "text_color": text,
            "from": {"zdc": leg["zfrom"], **f}, "to": {"zdc": leg["zto"], **t},
            "path": body["path"] if body else None,
            "length_m": body["length_m"] if body else None,
            "via": body["via"] if body else [],
            "source": body["source"] if body else "none",
        })

    # Estaciones en orden de paso, con su papel.
    order: list[str] = []
    for leg in legs:
        for z in (leg["zfrom"], leg["zto"]):
            if z and z not in order:
                order.append(z)
    route_stops: dict[str, list[str]] = {z: [] for z in order}     # zona -> stop_id
    platforms: dict[str, dict[str, dict]] = {z: {} for z in order}
    for leg in legs:
        body = bodies.get(leg["seq"])
        if not body:
            continue
        for side, z in (("from", leg["zfrom"]), ("to", leg["zto"])):
            if z not in route_stops:
                continue
            sid = body[side].get("stop_id")
            if sid and sid not in route_stops[z]:
                route_stops[z].append(sid)
            for p in body.get("platforms", {}).get(side, []):
                platforms[z].setdefault(p["arrid"], {
                    "id": f"STIF:StopPoint:Q:{p['arrid']}:", "line": leg["code"] or None,
                    "name": p.get("name"), "lat": p["lat"], "lon": p["lon"]})
    stations_out, accesses_out = [], []
    first = legs[0]["zfrom"] if legs else ""
    last = legs[-1]["zto"] if legs else ""
    for z in order:
        st = stations.get(z)
        role = "origin" if z == first else "destination" if z == last else "transfer"
        if st:
            name, lat, lon = st["name"], st["lat"], st["lon"]
        else:
            ln = next((x for x in lines if x["from"]["zdc"] == z), None)
            pt = ln["from"] if ln else next((x["to"] for x in lines if x["to"]["zdc"] == z), None)
            if not pt:
                continue
            name, lat, lon = pt["name"], pt["lat"], pt["lon"]
        zdas = []
        for sid in route_stops[z]:
            zda = _stop_zda(sid, st)
            if zda and zda not in zdas:
                zdas.append(zda)
        trk: list[dict] = []
        for zda in zdas:
            for t in tracks.get(zda) or []:
                if all(x["voie"] != t["voie"] for x in trk):
                    trk.append(t)
        stations_out.append({"zdc": z, "name": name, "lat": lat, "lon": lon, "role": role,
                             "platforms": list(platforms[z].values()), "tracks": trk})
        seen: dict[str, dict] = {}
        for zda in zdas:
            for a in acc.get(zda) or []:
                if a["id"] in seen:
                    continue
                to_stop = []
                served = {x for x in zdas if any(y["id"] == a["id"] for y in acc.get(x) or [])}
                gid = f"IDFM:StopPlaceEntrance:{a['id']}"
                for sid in route_stops[z]:
                    hits = [p for p in paths.get(sid) or [] if p[0] == gid]
                    if hits:
                        # Mejor el sentido acceso -> anden; si solo hay el de
                        # salida (un acceso que solo es salida), ese.
                        hits.sort(key=lambda p: (p[3] == "out", p[1] if p[1] is not None else math.inf))
                        m = hits[0][1]
                        to_stop.append({"stop_id": sid, "m": round(m, 2) if m is not None else None,
                                        "s": hits[0][2]})
                    elif _stop_zda(sid, st) in served:
                        # El acceso sirve a la zona de la parada (relations-acces)
                        # pero no hay camino en pathways.txt (o no se pudo
                        # leer): se dice, sin inventar la distancia.
                        to_stop.append({"stop_id": sid, "m": None, "s": None})
                seen[a["id"]] = {"id": a["id"], "zdc": z, "name": a["name"], "number": a["number"],
                                 "entry": a["entry"], "exit": a["exit"], "lat": a["lat"],
                                 "lon": a["lon"], "to_stop": to_stop}
        accesses_out.extend(seen.values())

    transfers = []
    for a, b in zip(legs, legs[1:]):
        ba, bb = bodies.get(a["seq"]), bodies.get(b["seq"])
        pair = ((ba or {}).get("to", {}).get("stop_id"), (bb or {}).get("from", {}).get("stop_id"))
        transfers.append({"zdc": a["zto"] or b["zfrom"], "from_seq": a["seq"], "to_seq": b["seq"],
                          "min_transfer_s": xfers.get(pair) if all(pair) else None})

    return {
        "route_id": int(route["id"]),
        "generated_at": _now_iso(),
        "pending": pending, "stale": stale,
        "lines": lines, "stations": stations_out, "accesses": accesses_out,
        "transfers": transfers,
        "sources": dict(sorted(sources.items())),
        "license": _license(sources),
    }


def _valid_route(body: dict) -> bool:
    try:
        if not all(_valid_leg(ln) for ln in body["lines"]):
            return False
        pts = [(s["lon"], s["lat"]) for s in body["stations"]]
        pts += [(a["lon"], a["lat"]) for a in body["accesses"]]
        pts += [(p["lon"], p["lat"]) for s in body["stations"] for p in s["platforms"] + s["tracks"]]
        json.dumps(body)
        return all(_in_idf(lon, lat) for lon, lat in pts)
    except (KeyError, TypeError, ValueError):
        return False


async def _build(route: dict, mode: str, catalog: dict | None = None) -> tuple[dict, _Ctx]:
    """Monta el mapa. mode=cache: solo lo guardado, sin red ni escrituras."""
    ctx = _Ctx(mode, catalog)
    legs = _legs_of(route)
    await _load_catalog(ctx)
    codes = list(dict.fromkeys(x["code"] for x in legs if x["code"]))
    zdcs = list(dict.fromkeys(z for x in legs for z in (x["zfrom"], x["zto"]) if z))
    refs = await _lines(ctx, codes)
    for leg in legs:
        ref = refs.get(leg["code"])
        leg["mode"] = _mode(ref, leg)
        leg["short"] = (ref or {}).get("short") or leg["line_code"]
    stations, hashes = await _stations(ctx, zdcs)
    bodies = {}
    for leg in legs:
        b = await _leg(ctx, leg, stations, hashes)
        if b is not None:
            bodies[leg["seq"]] = b
    # ZdA que usa la ruta en cada estacion (las de sus paradas de subida y bajada).
    zdas: list[str] = []
    rail: dict[str, list[str]] = {}
    stop_ids: set[str] = set()
    for leg in legs:
        b = bodies.get(leg["seq"])
        for side, z in (("from", leg["zfrom"]), ("to", leg["zto"])):
            sid = (b or {}).get(side, {}).get("stop_id")
            zda = _stop_zda(sid, stations.get(z))
            if sid:
                stop_ids.add(sid)
            if zda and zda not in zdas:
                zdas.append(zda)
                st = stations[z]
                if st["zdas"].get(zda, {}).get("type") == "railStation":
                    arr = [a for a, x in st["arrets"].items() if x["zda"] == zda and x["type"] == "rail"]
                    if arr:
                        rail[zda] = arr
    acc = await _accesses(ctx, zdas) if zdas else {}
    trk = await _tracks(ctx, rail) if rail else {}
    pairs = set()
    for a, b in zip(legs, legs[1:]):
        x = (bodies.get(a["seq"]) or {}).get("to", {}).get("stop_id")
        y = (bodies.get(b["seq"]) or {}).get("from", {}).get("stop_id")
        if x and y and x != y:
            pairs.add((x, y))
    paths, xfers = await _gtfs(ctx, stop_ids, pairs) if (stop_ids or pairs) else ({}, {})
    body = await run_in_threadpool(
        _assemble, route, legs, refs, stations, bodies, acc, trk, paths, xfers,
        ctx.sources, bool(ctx.errors), False)
    return body, ctx


# =====================================================================
# Cola, refresco y estado
# =====================================================================

_sem: asyncio.Semaphore | None = None
_loop: asyncio.AbstractEventLoop | None = None
_queued: dict[int, asyncio.Task] = {}
_again: set[int] = set()
_retry: set[int] = set()
_refresher_task: asyncio.Task | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(1)     # un solo calculo a la vez (§8.8)
    return _sem


def _quality(body: dict) -> tuple:
    """Para no cambiar un mapa por otro peor: trazados de verdad, trazados
    (aunque sean rectas), estaciones, accesos y distancias conocidas."""
    lines = body.get("lines") or []
    return (sum(1 for x in lines if x.get("source") in ("gtfs", "ferre")),
            sum(1 for x in lines if x.get("path")),
            len(body.get("stations") or []), len(body.get("accesses") or []),
            sum(1 for a in body.get("accesses") or [] for t in a.get("to_stop") or []
                if t.get("m") is not None))


async def _compute(route: dict, mode: str = "compute", catalog: dict | None = None) -> dict:
    """Calcula y guarda el mapa de una ruta (sin sustituir lo bueno por algo
    peor). Devuelve lo que se sirve."""
    async with _semaphore():
        key = f"route:{int(route['id'])}"
        fp = fingerprint(route)
        body, ctx = await _build(route, mode, catalog)
        prev = (await _rows([key])).get(key)
        same = prev is not None and prev["raw_hash"] == fp
        if ctx.errors:
            _retry.add(int(route["id"]))
            if same and _quality(prev["body"]) > _quality(body):
                # Lo guardado es mejor que lo que ha salido: se sirve, marcado viejo.
                old = prev["body"]
                old["stale"] = True
                ctx.put(key, old, fp, prev["source_version"])
                body = old
            elif _valid_route(body):
                # Mejor o igual (cada pieza que fallo ya salio de lo guardado):
                # vale, pero tambien con `stale` para que la app lo sepa.
                ctx.put(key, body, fp, "")
            await _save_state(error="; ".join(ctx.errors)[:500])
        else:
            _retry.discard(int(route["id"]))
            if _valid_route(body):
                ctx.put(key, body, fp, "")
            else:
                log.warning("mapa: la ruta %s no pasa la validacion; se conserva lo anterior", route["id"])
                if same:
                    body = prev["body"]
            await _save_state(ok=True)
        await run_in_threadpool(_db_put, ctx.writes)
        return body


async def _save_state(ok: bool = False, error: str | None = None) -> None:
    def save():
        cur = (_db_get(["meta:state"]).get("meta:state") or {}).get("body") or {}
        if ok:
            cur["last_refresh"] = _now_iso()
            cur["last_error"] = None
        if error:
            cur["last_error"] = error
        _db_put([("meta:state", cur, "", "")])
    try:
        await run_in_threadpool(save)
    except Exception as e:           # el estado del panel nunca tumba el calculo
        log.warning("mapa: no se pudo guardar el estado: %s", e)


async def _run_route(route_id: int) -> None:
    try:
        while True:
            _again.discard(route_id)
            route = await run_in_threadpool(db.get_route, route_id, True)
            if route:
                await _compute(route, "compute")
            if route_id not in _again:
                break
    except Exception as e:
        log.warning("mapa: fallo al calcular la ruta %s: %s", route_id, e)
    finally:
        if _queued.get(route_id) is asyncio.current_task():
            _queued.pop(route_id, None)


def _enqueue(route_id: int) -> None:
    t = _queued.get(route_id)
    if t is not None and not t.done():
        _again.add(route_id)          # se recalcula al acabar, con la ruta nueva
        return
    _queued[route_id] = asyncio.get_running_loop().create_task(_run_route(route_id))


def schedule_route(route_id: int) -> None:
    """Encola el calculo del mapa de una ruta (al crearla o editarla). Vuelve
    enseguida; se puede llamar desde el bucle o desde un hilo."""
    if not settings.map_enabled:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if _loop is not None and not _loop.is_closed():
            _loop.call_soon_threadsafe(_enqueue, int(route_id))
        return
    _enqueue(int(route_id))


async def wait_idle() -> None:
    """Espera a que acaben los calculos encolados (tests y apagado)."""
    while _queued:
        await asyncio.gather(*list(_queued.values()), return_exceptions=True)


async def refresh_all(full: bool = True) -> None:
    """Comprobacion de frescura (§8.7): UNA llamada al catalogo y solo se
    vuelve a pedir lo que tenga `data_processed` nuevo (y el hash del dato
    crudo evita recalcular). Con full=False solo reintenta las rutas que
    fallaron."""
    routes = await run_in_threadpool(db.list_routes)
    ids = [int(r["id"]) for r in routes]
    todo = ids if full else [i for i in ids if i in _retry]
    if not todo:
        # Sin rutas no se pide nada al portal (solo lo que usan las rutas).
        await run_in_threadpool(_db_prune_routes, ids)
        return
    try:
        catalog = await _portal.catalog(CATALOG_DATASETS)
    except PortalError as e:
        log.warning("mapa: catalogo no disponible: %s", e)
        await _save_state(error=str(e))
        return
    await run_in_threadpool(_db_put, [(f"src:{d}", {"data_processed": dp}, "", "")
                                      for d, dp in catalog.items()])
    for rid in todo:
        route = await run_in_threadpool(db.get_route, rid, True)
        if route:
            await _compute(route, "refresh", catalog)
    await run_in_threadpool(_db_prune_routes, ids)


def _seconds_until_daily() -> float:
    now = datetime.now(settings.tz)
    nxt = now.replace(hour=REFRESH_AT[0], minute=REFRESH_AT[1], second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


async def _refresher() -> None:
    """Tarea de fondo: al arrancar, las rutas sin mapa (y el refresco si el
    ultimo fue hace mas de un dia); despues, cada dia a las 03:30 de Paris y,
    si algo fallo, un reintento tras la pausa."""
    try:
        await asyncio.sleep(STARTUP_DELAY)
        state = await run_in_threadpool(
            lambda: (_db_get(["meta:state"]).get("meta:state") or {}).get("body") or {})
        if _age_days(state.get("last_refresh")) >= 1:
            await refresh_all(full=True)
        routes = await run_in_threadpool(db.list_routes)
        have = await _rows([f"route:{r['id']}" for r in routes])
        for r in routes:
            if f"route:{r['id']}" not in have:
                schedule_route(int(r["id"]))
        while True:
            daily = _seconds_until_daily()
            retry = bool(_retry) and BREAKER_PAUSE < daily
            await asyncio.sleep(BREAKER_PAUSE if retry else daily)
            try:
                await refresh_all(full=not retry)
            except Exception as e:
                log.warning("mapa: fallo en el refresco: %s", e)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("mapa: el refresco se ha parado: %s", e)


async def startup() -> None:
    """Arranca el refresco diario en segundo plano (nada si el mapa esta apagado)."""
    global _loop, _refresher_task
    if not settings.map_enabled:
        return
    _loop = asyncio.get_running_loop()
    _semaphore()
    if _refresher_task is None or _refresher_task.done():
        _refresher_task = _loop.create_task(_refresher())


async def shutdown() -> None:
    global _refresher_task, _loop, _sem
    tasks = [t for t in [_refresher_task, *_queued.values()] if t is not None and not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _queued.clear()
    _again.clear()
    _refresher_task = None
    _loop = None
    _sem = None
    await _portal.close()


async def route_map(route: dict) -> dict:
    """RouteMap de una ruta (`db.get_route(id, with_coords=True)`).

    Nunca espera a la red: lo guardado si corresponde a la ruta tal como esta;
    si no (ruta nueva o editada), lo que haya con `pending: true` y el calculo
    encolado.
    """
    key = f"route:{int(route['id'])}"
    row = (await _rows([key])).get(key)
    if row is not None and row["raw_hash"] == fingerprint(route):
        body = row["body"]
        body["pending"] = False
        return body
    if settings.map_enabled:
        schedule_route(int(route["id"]))
    body, _ = await _build(route, "cache")
    body["pending"] = bool(settings.map_enabled)
    body["stale"] = False
    return body


def status() -> dict:
    """Para el panel (AdminOverview.map_data). Lee SQLite: desde el bucle de
    eventos, llamarlo con run_in_threadpool."""
    try:
        n = _db_count()
        st = (_db_get(["meta:state"]).get("meta:state") or {}).get("body") or {}
    except Exception as e:
        return {"cached_items": 0, "last_refresh": None, "last_error": f"BD: {e}"}
    return {"cached_items": n, "last_refresh": st.get("last_refresh"),
            "last_error": st.get("last_error")}


def _reset() -> None:
    """Solo para tests: olvida el estado en memoria (cliente, pausas, colas)."""
    global _portal, _sem, _loop, _refresher_task
    _portal = _Portal()
    _sem = None
    _loop = None
    _refresher_task = None
    _queued.clear()
    _again.clear()
    _retry.clear()
