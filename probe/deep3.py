import json, sys
from collections import defaultdict
rows=[json.loads(l) for l in open(sys.argv[1],encoding="utf-8") if l.strip()]
d=defaultdict(lambda:[0,0])
for r in rows:
    k=r["line"]; d[k][1]+=1
    if r.get("aimed"): d[k][0]+=1
print("=== Lineas con AimedDepartureTime (base para calcular retraso) ===")
sin=[]
for k,(a,n) in sorted(d.items(), key=lambda x:-x[1][1])[:18]:
    print(f"  {k:24} {a:6}/{n:6}  {100*a/n:5.1f}%")
    if a==0: sin.append(k)
print(f"\n  lineas sin 'aimed' en absoluto: {sum(1 for a,n in d.values() if a==0)} de {len(d)}")
