import json, urllib.request, urllib.parse
B="http://localhost:7796"
def g(p):
    return json.load(urllib.request.urlopen(B+p, timeout=30))

r = g("/api/search/stops?q="+urllib.parse.quote("Saint-Lazare"))
print("=== busqueda 'Saint-Lazare' ===")
for s in r.get("stops", [])[:8]:
    print("  ", s)
sid = r["stops"][0]["id"]
print("\n=== lineas en", sid, "===")
l = g("/api/stops/"+urllib.parse.quote(sid)+"/lines")
for x in l["lines"]:
    print(f'  {x["mode"]:<14} {str(x["code"]):<6} {x["name"][:40]}')
print("  total:", len(l["lines"]))
