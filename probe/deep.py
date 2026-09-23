import json, sys, statistics as st
from collections import defaultdict

rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]

def real(v):
    if not v: return None
    v = str(v).strip()
    if not v or v.lower() in ("unknown","none","null"): return None
    if v.upper().startswith("PARIS"): return None
    return v

# 1) que lineas llegan a publicar anden
por_linea = defaultdict(lambda: {"trenes": set(), "con": set()})
for r in rows:
    ln, jid = r.get("line"), r.get("jid")
    if not jid: continue
    por_linea[ln]["trenes"].add(jid)
    if real(r.get("pdep")) or real(r.get("parr")):
        por_linea[ln]["con"].add(jid)

print("=== Por linea: trenes con anden / trenes vistos ===")
for ln, d in sorted(por_linea.items(), key=lambda x: -len(x[1]["con"])):
    n, c = len(d["trenes"]), len(d["con"])
    if n < 3: continue
    print(f"  {ln:26} {c:4}/{n:4}  {100*c/n:5.1f}%")

# 2) frescura del feed segun lo cerca que esta la salida
import datetime as dt
def parse(s):
    if not s: return None
    try: return dt.datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception: return None

buckets = defaultdict(list)
for r in rows:
    sm, rec = parse(r.get("sample_at")), parse(r.get("rec"))
    sal = parse(r.get("exp")) or parse(r.get("aimed"))
    if not (sm and rec and sal): continue
    falta = (sal - sm).total_seconds()/60
    edad  = (sm - rec).total_seconds()/60
    if falta < 0: continue
    b = "0-10 min" if falta<10 else "10-30 min" if falta<30 else "30-60 min" if falta<60 else ">60 min"
    buckets[(r.get("station"),b)].append(edad)

print("\n=== Edad del dato (sample - RecordedAtTime) segun cuanto falta para la salida ===")
for k in sorted(buckets, key=lambda k:(k[0], ["0-10 min","10-30 min","30-60 min",">60 min"].index(k[1]))):
    v = sorted(buckets[k])
    print(f"  {k[0]:14} {k[1]:10} n={len(v):6}  mediana={st.median(v):8.1f} min  p90={v[int(.9*len(v))-1]:8.1f}")
