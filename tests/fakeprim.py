"""PRIM falso para los tests: un httpx.MockTransport con la forma real de SIRI
y Navitia.

Se monta con `prim.transport_override = fake.transport` (lo hace conftest.py).
Cuenta las llamadas por endpoint, pone la cabecera de cuota y puede fallar a
demanda (401, 403, 429, 5xx, timeout) por endpoint.

Los escenarios de `scenarios.py` reproducen los casos de PreviewData de la app
(sin via, via probable, via que aparece, bus a 106 min, linea cortada, aviso
en frances, tren en el anden, destinos mezclados, tramo vacio).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx

SIRI_PREFIX = "/marketplace"
NAVITIA_PREFIX = "/marketplace/v2/navitia"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class Dep:
    """Un paso en una estacion, en minutos desde ahora."""
    line: str                      # codigo de linea, p. ej. "C01739"
    dest: str
    minutes: float
    aimed_delta: float | None = 0  # retraso en minutos; None = sin hora teorica
    platform: str | None = None    # via real; "unknown" para imitar Saint-Lazare
    train: str | None = None
    length: str | None = None      # "shortTrain" | "longTrain" | None
    at_stop: bool = False
    status: str = "onTime"
    jid: str | None = None
    arrival_only: bool = False     # solo ExpectedArrivalTime (fin de linea)


@dataclass
class Msg:
    """Un aviso de general-message."""
    lines: list[str]               # codigos de linea afectados
    text: str
    valid_hours: float = 6
    short: str | None = None
    via_item_identifier: bool = False


@dataclass
class FakePrim:
    stations: dict[str, list[Dep]] = field(default_factory=dict)   # zdc -> pasos
    messages: list[Msg] = field(default_factory=list)
    places: dict[str, list[dict]] = field(default_factory=dict)    # q -> places
    lines_at: dict[str, list[dict]] = field(default_factory=dict)  # stop_area -> lines
    journeys_payload: dict | None = None
    calls: dict[str, int] = field(default_factory=dict)
    remaining: dict[str, int] = field(default_factory=lambda: {
        "stop-monitoring": 900, "general-message": 950, "navitia": 980})
    # endpoint -> estado HTTP (401/403/429/500) o "timeout"
    fail: dict[str, int | str] = field(default_factory=dict)
    seen_keys: list[str] = field(default_factory=list)
    now: datetime | None = None

    # ---------------- construir el mundo ----------------

    def add(self, station: str, *deps: Dep) -> "FakePrim":
        self.stations.setdefault(str(station), []).extend(deps)
        return self

    def message(self, *msgs: Msg) -> "FakePrim":
        self.messages.extend(msgs)
        return self

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def total_calls(self) -> int:
        return sum(self.calls.values())

    # ---------------- respuestas ----------------

    def _now(self) -> datetime:
        return self.now or datetime.now(timezone.utc)

    def _bucket(self, path: str) -> str:
        if path.startswith(NAVITIA_PREFIX):
            return "navitia"
        if path.endswith("/stop-monitoring"):
            return "stop-monitoring"
        if path.endswith("/general-message"):
            return "general-message"
        return "other"

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        bucket = self._bucket(path)
        self.calls[bucket] = self.calls.get(bucket, 0) + 1
        self.seen_keys.append(request.headers.get("apikey", ""))

        f = self.fail.get(bucket) or self.fail.get("*")
        if f == "timeout":
            raise httpx.ReadTimeout("tiempo de espera agotado", request=request)
        if f == "connect":
            raise httpx.ConnectError("no se puede conectar", request=request)
        headers = {}
        if bucket in self.remaining:
            self.remaining[bucket] = max(0, self.remaining[bucket] - 1)
            headers["x-ratelimit-remaining-day"] = str(self.remaining[bucket])
        if isinstance(f, int):
            body = {"message": {401: "Invalid authentication credentials",
                                403: "You cannot consume this service",
                                429: "API rate limit exceeded"}.get(f, "error")}
            return httpx.Response(f, json=body, headers=headers)

        q = parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes)
                     else str(request.url.query))
        if bucket == "stop-monitoring":
            ref = (q.get("MonitoringRef") or [""])[0]
            return httpx.Response(200, json=self._stop_monitoring(ref), headers=headers)
        if bucket == "general-message":
            return httpx.Response(200, json=self._general_message(), headers=headers)
        if bucket == "navitia":
            return httpx.Response(200, json=self._navitia(path, q), headers=headers)
        return httpx.Response(404, json={"message": "no encontrado"})

    def _stop_monitoring(self, ref: str) -> dict:
        # STIF:StopArea:SP:71370: -> 71370
        zdc = ref.rstrip(":").rsplit(":", 1)[-1]
        now = self._now()
        visits = []
        for i, d in enumerate(self.stations.get(zdc, [])):
            expected = now + timedelta(minutes=d.minutes)
            call = {"VehicleAtStop": d.at_stop, "DepartureStatus": d.status,
                    "DestinationDisplay": [{"value": d.dest}]}
            if d.arrival_only:
                call["ExpectedArrivalTime"] = iso(expected)
            else:
                call["ExpectedDepartureTime"] = iso(expected)
            if d.aimed_delta is not None:
                aimed = expected - timedelta(minutes=d.aimed_delta)
                call["AimedDepartureTime" if not d.arrival_only else "AimedArrivalTime"] = iso(aimed)
            if d.platform is not None:
                call["DeparturePlatformName"] = {"value": d.platform}
            mvj = {
                "LineRef": {"value": f"STIF:Line::{d.line}:"},
                "DestinationName": [{"value": d.dest}],
                "FramedVehicleJourneyRef": {
                    "DatedVehicleJourneyRef": d.jid or f"SNCF:VJ:{zdc}:{d.line}:{i}:{d.dest}"},
                "MonitoredCall": call,
            }
            if d.train:
                mvj["TrainNumbers"] = {"TrainNumberRef": [{"value": d.train}]}
            if d.length:
                mvj["VehicleFeatureRef"] = [{"value": d.length}]
            visits.append({"ItemIdentifier": f"item-{zdc}-{i}",
                           "MonitoringRef": {"value": ref},
                           "MonitoredVehicleJourney": mvj})
        return {"Siri": {"ServiceDelivery": {
            "ResponseTimestamp": iso(now),
            "StopMonitoringDelivery": [{"MonitoredStopVisit": visits}]}}}

    def _general_message(self) -> dict:
        now = self._now()
        infos = []
        for i, m in enumerate(self.messages):
            content = {"Message": [{"MessageText": {"value": m.text}}]}
            if m.short:
                content["Message"].append({"MessageText": {"value": m.short}})
            item = f"IDFM:MSG:{i}"
            if m.via_item_identifier:
                item = "RATP:MSG.SAE-BUS." + ".".join(f"IDFM.{c}" for c in m.lines) + f".{i}"
            else:
                content["LineRef"] = [{"value": f"STIF:Line::{c}:"} for c in m.lines]
            infos.append({"ItemIdentifier": item,
                          "ValidUntilTime": iso(now + timedelta(hours=m.valid_hours)),
                          "Content": content})
        return {"Siri": {"ServiceDelivery": {
            "ResponseTimestamp": iso(now),
            "GeneralMessageDelivery": [{"InfoMessage": infos}]}}}

    def _navitia(self, path: str, q: dict) -> dict:
        rest = path[len(NAVITIA_PREFIX):]
        if rest.startswith("/places"):
            text = (q.get("q") or [""])[0].lower().strip()
            return {"places": self.places.get(text, [])}
        if rest.startswith("/stop_areas/") and rest.endswith("/lines"):
            sa = rest[len("/stop_areas/"):-len("/lines")]
            return {"lines": self.lines_at.get(sa, [])}
        if rest.startswith("/journeys"):
            return self.journeys_payload or {"journeys": []}
        if rest.startswith("/coverage"):
            return {"regions": [{"id": "IDFM", "status": "running"}]}
        return {}


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
