"""Portal de IDFM falso para los tests del mapa: un httpx.MockTransport que
sirve las muestras reales de tests/fixtures/idfm/ (ver su README.md).

- data.iledefrance-mobilites.fr: catalogo y `exports/<fmt>` de cada dataset,
  filtrando por las clausulas sencillas del `where` (`campo='x'`,
  `campo in (...)`) como haria el portal.
- eu.ftp.opendatasoft.com: el zip GTFS montado en memoria con las filas
  reales de `gtfs-*.txt`, con ETag, 304 y peticiones Range como el de verdad.

Apunta todas las peticiones (para comprobar R90 y que nunca hay volcados
completos) y puede caerse entero o por dataset.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import zipfile
from urllib.parse import parse_qs, urlsplit

import httpx

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "idfm")

GTFS_ETAG = '"6ab3e0e9-7dda5cc"'
GTFS_LAST_MODIFIED = "Wed, 23 Sep 2026 14:23:37 GMT"

# Filas reales de `arrets-lignes` que no estan en las muestras, pedidas al
# portal el 24/09/2026 sin clave (prueba de humo del modulo del mapa):
#   records?where=id="IDFM:C01254" and stop_id in (<postes de Gare d'Argenteuil>)
#   records?where=id="IDFM:C01254" and stop_name like "Jean Moulin%"
BUS_272_STOPS = [
    {"id": "IDFM:C01254", "stop_id": "IDFM:40000", "stop_name": "Gare d'Argenteuil",
     "stop_lat": "48.946242054857784", "stop_lon": "2.2577112088722266", "mode": "Bus"},
    {"id": "IDFM:C01254", "stop_id": "IDFM:39839", "stop_name": "Gare d'Argenteuil",
     "stop_lat": "48.94629457001942", "stop_lon": "2.2574783590465004", "mode": "Bus"},
    {"id": "IDFM:C01254", "stop_id": "IDFM:40068", "stop_name": "Jean Moulin - Henri Barbusse",
     "stop_lat": "48.93869385820138", "stop_lon": "2.242830150410148", "mode": "Bus"},
    {"id": "IDFM:C01254", "stop_id": "IDFM:23986", "stop_name": "Jean Moulin - Henri Barbusse",
     "stop_lat": "48.93832216226253", "stop_lon": "2.242357779635877", "mode": "Bus"},
]


def fixture(name: str):
    path = os.path.join(FIX, name)
    if name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def gtfs_rows(name: str) -> list[str]:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read().splitlines()


def build_gtfs_zip() -> bytes:
    """Un zip con los tres ficheros del GTFS de las muestras (filas reales).
    Como el de verdad, lleva otros miembros que no se deben leer."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("agency.txt", "agency_id,agency_name\nIDFM:1,Île-de-France Mobilités\n")
        for member, fixture_name in (("stops.txt", "gtfs-stops-saint-lazare-argenteuil.txt"),
                                     ("pathways.txt", "gtfs-pathways-saint-lazare-argenteuil.txt"),
                                     ("transfers.txt", "gtfs-transfers-saint-lazare-argenteuil.txt")):
            z.writestr(member, "\r\n".join(gtfs_rows(fixture_name)) + "\r\n")
        # Relleno poco comprimible, como stop_times.txt: bajar el zip entero
        # se notaria en los bytes servidos.
        z.writestr("stop_times.txt", "trip_id,stop_id\n" + "".join(
            f"IDFM:{i * 7919 % 1000003},IDFM:{i * 104729 % 999983}\n" for i in range(20000)))
    return buf.getvalue()


_IN = re.compile(r"(\w+)\s+in\s*\(([^)]*)\)")
_EQ = re.compile(r"(\w+)\s*=\s*'([^']*)'")


def _filters(where: str) -> list[tuple[str, set[str]]]:
    """Clausulas `and` sencillas. Con `or` no se filtra (se sirve la muestra
    tal cual, que ya es la respuesta real a esa consulta)."""
    if not where or " or " in where:
        return []
    out = []
    for field, vals in _IN.findall(where):
        out.append((field, {v.strip().strip("'\"") for v in vals.split(",")}))
    for field, val in _EQ.findall(where):
        out.append((field, {val}))
    return out


def _match(rec: dict, filters) -> bool:
    for field, vals in filters:
        if field in rec and str(rec[field]) not in vals:
            return False
    return True


class FakePortal:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.down = False                    # el portal no responde (conexion)
        self.gtfs_down = False
        self.status: dict[str, int] = {}     # dataset -> estado HTTP forzado
        self.override: dict[str, object] = {}  # dataset -> respuesta
        self.catalog = fixture("catalogo-frescura.json")
        self.gtfs_zip = build_gtfs_zip()
        self.gtfs_ignores_range = False
        self.gtfs_bytes = 0                  # bytes del zip servidos
        self.transport = httpx.MockTransport(self.handler)

    # ---------------- utilidades para los tests ----------------

    def portal_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "data.iledefrance-mobilites.fr"]

    def gtfs_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "eu.ftp.opendatasoft.com"]

    def exports(self, dataset: str | None = None) -> list[httpx.Request]:
        return [r for r in self.portal_requests() if "/exports/" in r.url.path
                and (dataset is None or f"/datasets/{dataset}/" in r.url.path)]

    def set_processed(self, dataset: str, when: str) -> None:
        for x in self.catalog["results"]:
            if x["dataset_id"] == dataset:
                x["data_processed"] = x["modified"] = when

    # ---------------- el servidor ----------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "eu.ftp.opendatasoft.com":
            return self._gtfs(request)
        if self.down:
            raise httpx.ConnectError("portal caido (test)", request=request)
        path = request.url.path
        q = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if path.rstrip("/").endswith("/catalog/datasets"):
            ids = {v for _, vals in _filters(q.get("where", "")) for v in vals}
            res = [x for x in self.catalog["results"] if not ids or x["dataset_id"] in ids]
            return httpx.Response(200, json={"total_count": len(res), "results": res})
        m = re.search(r"/catalog/datasets/([^/]+)/exports/(json|geojson)$", path)
        if not m:
            return httpx.Response(404, json={"error_code": "NotFound"})
        dataset, fmt = m.groups()
        if dataset in self.status:
            return httpx.Response(self.status[dataset], json={"error_code": "Test"})
        data = self.override.get(dataset)
        if data is None:
            data = self._dataset(dataset, q.get("where", ""))
        filters = _filters(q.get("where", ""))
        if isinstance(data, dict) and "features" in data:
            data = {"type": "FeatureCollection",
                    "features": [f for f in data["features"] if _match(f.get("properties") or {}, filters)]}
        elif isinstance(data, list):
            data = [r for r in data if _match(r, filters)]
        return httpx.Response(200, json=data)

    def _dataset(self, dataset: str, where: str):
        empty = {"type": "FeatureCollection", "features": []}
        if dataset == "referentiel-des-lignes":
            return fixture("referentiel-lignes-J.json") + fixture("referentiel-lignes-14-272.json")
        if dataset == "arrets-lignes":
            if "C01739" in where:
                return fixture("arrets-lignes-J.json")
            if "C01254" in where:
                return BUS_272_STOPS
            return []
        if dataset == "traces-des-lignes-de-transport-en-commun-idfm":
            for code, name in (("C01739", "traces-gtfs-ligne-J.geojson.gz"),
                               ("C01384", "traces-gtfs-metro-14.geojson.gz"),
                               ("C01254", "traces-gtfs-bus-272.geojson.gz")):
                if code in where:
                    return fixture(name)
            return empty
        if dataset == "traces-du-reseau-ferre-idf":
            return fixture("traces-ferre-J-saint-lazare-argenteuil.geojson") if "C01739" in where else empty
        if dataset == "emplacement-des-gares-idf":
            return fixture("gares-saint-lazare-argenteuil.geojson")
        if dataset == "zones-de-correspondance":
            return fixture("zdc-saint-lazare-argenteuil.json")
        if dataset == "zones-d-arrets":
            return fixture("zda-saint-lazare-argenteuil.json")
        if dataset == "arrets":
            return {"type": "FeatureCollection",
                    "features": fixture("arrets-saint-lazare.geojson")["features"]
                    + fixture("arrets-argenteuil.geojson")["features"]}
        if dataset == "relations-acces":
            return fixture("relations-acces-saint-lazare-argenteuil.json")
        if dataset == "acces":
            return {"type": "FeatureCollection",
                    "features": fixture("acces-saint-lazare.geojson")["features"]
                    + fixture("acces-argenteuil.geojson")["features"]}
        if dataset == "arrets-transporteur":
            return fixture("arrets-transporteur-vias-saint-lazare.json")
        return []

    def _gtfs(self, request: httpx.Request) -> httpx.Response:
        if self.gtfs_down:
            raise httpx.ConnectError("GTFS caido (test)", request=request)
        data = self.gtfs_zip
        total = len(data)
        base = {"ETag": GTFS_ETAG, "Last-Modified": GTFS_LAST_MODIFIED, "Accept-Ranges": "bytes"}
        if request.headers.get("If-None-Match") == GTFS_ETAG:
            return httpx.Response(304, headers=base)
        rng = request.headers.get("Range", "")
        if_range = request.headers.get("If-Range")
        if self.gtfs_ignores_range or not rng or (if_range and if_range not in (GTFS_ETAG, GTFS_LAST_MODIFIED)):
            self.gtfs_bytes += total
            return httpx.Response(200, headers=base, content=data)
        m = re.match(r"bytes=(\d*)-(\d*)$", rng)
        if not m:
            return httpx.Response(416, headers=base)
        a, b = m.groups()
        if a == "":
            start, end = max(0, total - int(b)), total - 1
        else:
            start, end = int(a), min(total - 1, int(b) if b else total - 1)
        self.gtfs_bytes += end + 1 - start
        return httpx.Response(206, headers={**base, "Content-Range": f"bytes {start}-{end}/{total}"},
                              content=data[start:end + 1])
