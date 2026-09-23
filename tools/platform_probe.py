#!/usr/bin/env python3
"""
Sondeo del campo de anden en la API PRIM (SIRI stop-monitoring).

Pregunta que responde:
  1. DeparturePlatformName, durante servicio real, deja de valer "unknown"?
  2. Si se rellena, con cuanta antelacion respecto a la salida?
  3. En Gare du Nord, "PARIS NORD" acaba siendo un anden de verdad?

No instala nada: solo stdlib. Escribe un JSONL destilado (una linea por
salida y por muestra) para poder analizarlo despues sin volver a gastar cuota.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://prim.iledefrance-mobilites.fr/marketplace/stop-monitoring"

# (id de zona, etiqueta, cada cuantos segundos se muestrea)
STATIONS = [
    ("71370", "saint-lazare", 60),
    ("71410", "gare-du-nord", 120),
]

# Ventana de sondeo, en UTC. 05:00-07:00 UTC = 07:00-09:00 en Paris.
START_UTC = "2026-08-30T05:00:00Z"
STOP_UTC = "2026-08-30T07:00:00Z"

OBS = os.path.join(HERE, "probe_observations.jsonl")
LOG = os.path.join(HERE, "probe.log")


def log(msg):
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def load_key():
    """Lee PRIM_API_KEY del .env de al lado. Nunca la imprime."""
    env = os.path.join(HERE, ".env")
    if os.path.exists(env):
        with open(env, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if raw.startswith("PRIM_API_KEY="):
                    return raw.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("PRIM_API_KEY", "")


def first_value(node):
    """SIRI mezcla dict {'value':x} y list[{'value':x}] en el mismo sitio."""
    if isinstance(node, list):
        node = node[0] if node else None
    if isinstance(node, dict):
        return node.get("value")
    return node


def fetch(zone, key):
    url = f"{BASE}?MonitoringRef=STIF%3AStopArea%3ASP%3A{zone}%3A"
    req = urllib.request.Request(url, headers={"apikey": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        body = r.read().decode("utf-8")
        quota = r.headers.get("x-ratelimit-remaining-day")
        return json.loads(body), quota


def distill(payload, station, sampled_at):
    """Extrae solo lo necesario para responder la pregunta del anden."""
    out = []
    sd = payload.get("Siri", {}).get("ServiceDelivery", {})
    for delivery in sd.get("StopMonitoringDelivery", []):
        for visit in delivery.get("MonitoredStopVisit", []):
            mvj = visit.get("MonitoredVehicleJourney", {})
            mc = mvj.get("MonitoredCall", {})
            out.append({
                "t": sampled_at,
                "st": station,
                # Identidad estable del tren, para seguirlo entre muestras
                "jid": (mvj.get("FramedVehicleJourneyRef") or {}).get("DatedVehicleJourneyRef"),
                "ref": first_value(visit.get("MonitoringRef")),
                "line": first_value(mvj.get("LineRef")),
                "dest": first_value(mvj.get("DestinationName")),
                "train": first_value(mvj.get("TrainNumbers", {}).get("TrainNumberRef")),
                "aimed": mc.get("AimedDepartureTime"),
                "exp": mc.get("ExpectedDepartureTime"),
                # Lo que de verdad estamos midiendo
                "pdep": first_value(mc.get("DeparturePlatformName")),
                "parr": first_value(mc.get("ArrivalPlatformName")),
                "dstat": mc.get("DepartureStatus"),
                "atstop": mc.get("VehicleAtStop"),
                "rec": visit.get("RecordedAtTime"),
            })
    return out


def main():
    key = load_key()
    if not key:
        log("ABORTA: no hay PRIM_API_KEY")
        sys.exit(1)

    start = datetime.fromisoformat(START_UTC.replace("Z", "+00:00")).timestamp()
    stop = datetime.fromisoformat(STOP_UTC.replace("Z", "+00:00")).timestamp()

    log(f"sondeo programado {START_UTC} -> {STOP_UTC} | estaciones={[s[0] for s in STATIONS]}")
    wait = start - time.time()
    if wait > 0:
        log(f"esperando {wait/3600:.2f} h hasta el inicio")
        time.sleep(wait)

    next_due = {z: start for z, _, _ in STATIONS}
    calls = 0

    while time.time() < stop:
        now = time.time()
        for zone, label, every in STATIONS:
            if now < next_due[zone]:
                continue
            next_due[zone] = now + every
            sampled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                payload, quota = fetch(zone, key)
                rows = distill(payload, label, sampled_at)
                with open(OBS, "a", encoding="utf-8") as f:
                    for row in rows:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                calls += 1
                # Cuantos anden reales (ni ausente ni "unknown") en esta muestra
                real = sum(1 for r in rows
                           if r["pdep"] and str(r["pdep"]).lower() != "unknown")
                log(f"{label} ok | salidas={len(rows)} anden_real={real} | cuota_restante={quota}")
            except urllib.error.HTTPError as e:
                log(f"{label} HTTP {e.code} {e.reason}")
            except Exception as e:  # nunca morir a mitad del sondeo
                log(f"{label} ERROR {type(e).__name__}: {e}")
        time.sleep(5)

    log(f"sondeo terminado | {calls} llamadas")


if __name__ == "__main__":
    main()
