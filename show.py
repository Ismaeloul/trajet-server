import json, urllib.request
b = json.load(urllib.request.urlopen("http://localhost:7796/api/board", timeout=25))
print("ruta:", b["route"]["name"])
for l in b["legs"]:
    s = l["status"]
    print(f'  linea {l["line_code"]:>3} ({l["line_mode"]:<8}) -> {s["label"]:<12} nivel={s["level"]} planif={s["planned"]} salidas={len(l["departures"])}')
    for m in s["messages"][:1]:
        print("      ", m.replace("\n", " | ")[:115])
    for d in l["departures"][:4]:
        print(f'       {d["minutes"]:>3} min  {d["destination"][:26]:<26} anden={d["platform"] or "-":<4} retraso={d["delay"]}')
