#!/usr/bin/env python3
"""Analiza el JSONL del sondeo y responde: se rellena el anden, y cuando."""
import json
import sys
import collections
from datetime import datetime

def ts(s):
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def is_real(p):
    """Un anden de verdad: ni ausente, ni 'unknown', ni el nombre de la estacion."""
    if not p:
        return False
    v = str(p).strip()
    if v.lower() in ("unknown", "", "none"):
        return False
    # Gare du Nord publica "PARIS NORD" ahi, que es la estacion, no la via
    if v.upper() in ("PARIS NORD", "PARIS SAINT-LAZARE", "PARIS EST", "PARIS LYON"):
        return False
    return True

def main(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    if not rows:
        print("JSONL vacio")
        return

    samples = sorted({r["t"] for r in rows})
    print(f"Muestras: {len(samples)}  ({samples[0]} -> {samples[-1]})")
    print(f"Filas totales: {len(rows)}\n")

    print("=== Valores de DeparturePlatformName por estacion ===")
    per_st = collections.defaultdict(collections.Counter)
    for r in rows:
        per_st[r["st"]][r["pdep"]] += 1
    for st, c in per_st.items():
        print(f"  {st}:")
        for v, n in c.most_common(12):
            mark = "  <-- ANDEN REAL" if is_real(v) else ""
            print(f"      {n:6d}x  {v!r}{mark}")
    print()

    journeys = collections.defaultdict(list)
    for r in rows:
        journeys[(r["st"], r["jid"])].append(r)

    ever, never, leads = 0, 0, []
    for (st, jid), obs in journeys.items():
        obs.sort(key=lambda r: r["t"])
        first = next((o for o in obs if is_real(o["pdep"])), None)
        if not first:
            never += 1
            continue
        ever += 1
        dep = ts(first["exp"] or first["aimed"])
        seen = ts(first["t"])
        if dep and seen:
            leads.append(((dep - seen).total_seconds() / 60, st, first["line"],
                          first["dest"], first["pdep"], first["t"]))

    print(f"=== Trenes seguidos: {len(journeys)} ===")
    print(f"  con anden real en algun momento: {ever}")
    print(f"  nunca: {never}\n")

    if leads:
        leads.sort(reverse=True)
        vals = sorted(x[0] for x in leads)
        n = len(vals)
        print("=== Antelacion del anden (minutos antes de la salida) ===")
        print(f"  n={n}  min={vals[0]:.1f}  mediana={vals[n//2]:.1f}  max={vals[-1]:.1f}")
        print("\n  Primeras apariciones (mas antelacion arriba):")
        for lead, st, line, dest, plat, t in leads[:15]:
            print(f"    {lead:6.1f} min | {st:13s} | {line} -> {str(dest)[:22]:22s} | via {plat}")
    else:
        print("=== NINGUN anden real observado en toda la ventana ===")

    print("\n=== Frescura del feed (muestra - RecordedAtTime, en minutos) ===")
    for st in per_st:
        ages = []
        for r in rows:
            if r["st"] != st:
                continue
            a, b = ts(r["t"]), ts(r["rec"])
            if a and b:
                ages.append((a - b).total_seconds() / 60)
        if ages:
            ages.sort()
            print(f"  {st}: n={len(ages)} mediana={ages[len(ages)//2]:.1f} min  "
                  f"p90={ages[int(len(ages)*0.9)]:.1f} min  max={ages[-1]:.1f} min")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "probe_observations.jsonl")
