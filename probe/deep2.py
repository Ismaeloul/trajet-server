import json, sys, datetime as dt, statistics as st
from collections import defaultdict

rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
def parse(s):
    if not s: return None
    try: return dt.datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception: return None
def real(v):
    if not v: return None
    v=str(v).strip()
    if not v or v.lower() in ("unknown","none","null") or v.upper().startswith("PARIS"): return None
    return v

W0, W1 = parse("2026-08-30T05:00:00Z"), parse("2026-08-30T07:00:00Z")

# --- cobertura honesta: solo trenes cuya salida cae bien dentro de la ventana
trenes = {}
for r in rows:
    jid = r.get("jid")
    if not jid: continue
    sal = parse(r.get("exp")) or parse(r.get("aimed"))
    d = trenes.setdefault(jid, {"sal": sal, "st": r["st"], "line": r["line"], "and": False, "modo": None})
    if sal: d["sal"] = sal
    if real(r.get("pdep")) or real(r.get("parr")): d["and"] = True

dentro = [d for d in trenes.values() if d["sal"] and W0 + dt.timedelta(minutes=15) < d["sal"] < W1]
print(f"=== Trenes con salida totalmente observable ({len(dentro)}) ===")
for stn in ("saint-lazare","gare-du-nord"):
    sub=[d for d in dentro if d["st"]==stn]
    con=sum(1 for d in sub if d["and"])
    print(f"  {stn:14} {con:4}/{len(sub):4} con anden = {100*con/max(len(sub),1):5.1f}%")

# --- frescura segun cuanto falta para la salida
buckets=defaultdict(list)
for r in rows:
    sm, rec = parse(r["t"]), parse(r.get("rec"))
    sal = parse(r.get("exp")) or parse(r.get("aimed"))
    if not (sm and rec and sal): continue
    falta=(sal-sm).total_seconds()/60
    if falta<0: continue
    b = "0-10 min" if falta<10 else "10-30 min" if falta<30 else "30-60 min" if falta<60 else ">60 min"
    buckets[(r["st"],b)].append((sm-rec).total_seconds()/60)

print("\n=== Edad del dato (muestra - RecordedAtTime) segun cuanto falta para la salida ===")
orden=["0-10 min","10-30 min","30-60 min",">60 min"]
for k in sorted(buckets, key=lambda k:(k[0], orden.index(k[1]))):
    v=sorted(buckets[k])
    print(f"  {k[0]:14} {k[1]:10} n={len(v):6}  mediana={st.median(v):7.1f} min  p90={v[int(.9*len(v))-1]:7.1f}")

# --- retrasos observados
ret=[]
for r in rows:
    a,e=parse(r.get("aimed")),parse(r.get("exp"))
    if a and e: ret.append((e-a).total_seconds()/60)
print(f"\n=== Retraso (exp - aimed) === n={len(ret)}")
if ret:
    ret.sort()
    print(f"  mediana={st.median(ret):.1f} min  p90={ret[int(.9*len(ret))-1]:.1f}  max={ret[-1]:.1f}  con aimed={100*len(ret)/len(rows):.1f}% de las filas")
