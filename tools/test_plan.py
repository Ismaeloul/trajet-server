"""Prueba de extremo a extremo del planificador contra la API real.

Gasta cuota de Navitia (unas 4 llamadas), asi que no es para ejecutarla en
bucle. Se lanza contra una instancia ya levantada:

    TRAJET_URL=http://192.168.1.188:7796 python3 tools/test_plan.py
"""
import json, os, sys, urllib.parse, urllib.request

B = os.environ.get("TRAJET_URL", "http://localhost:7796")
FAILS = []


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'MAL'} {name}" + ("" if cond else f"   <<< {extra}"))
    if not cond:
        FAILS.append(name)


def get(path):
    return json.load(urllib.request.urlopen(B + path, timeout=40))


def post(path, body):
    req = urllib.request.Request(
        B + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))


print("=== orden de lineas en Saint-Lazare ===")
l = get("/api/stops/" + urllib.parse.quote("stop_area:IDFM:71370") + "/lines")["lines"]
primeras = [x["mode"] for x in l[:6]]
check("el metro sale primero, no el bus", all("tro" in m for m in primeras[:4]), primeras)
codigos = [x["code"] for x in l if "tro" in x["mode"]]
check("y en orden numerico: 3 antes que 12", codigos == ["3", "12", "13", "14"], codigos)
check("los buses de sustitucion, al final",
      "Remplacement" in (l[-1]["name"] or ""), l[-1]["name"])

print("\n=== buscar una direccion postal ===")
p = get("/api/search/places?q=" + urllib.parse.quote("12 rue de Rivoli"))["places"]
check("devuelve resultados", len(p) > 0)
check("hay al menos una direccion", any(x["kind"] == "dirección" for x in p),
      [x["kind"] for x in p])
addr = next(x for x in p if x["kind"] == "dirección")
print("     ->", addr["name"], "|", addr["id"])

print("\n=== buscar tambien paradas en el mismo buscador ===")
p2 = get("/api/search/places?q=" + urllib.parse.quote("Saint-Lazare"))["places"]
check("aparecen paradas", any(x["kind"] == "parada" for x in p2),
      [x["kind"] for x in p2][:6])

print("\n=== planificar de direccion a parada ===")
plan = get("/api/plan?from=" + urllib.parse.quote(addr["id"])
           + "&to=" + urllib.parse.quote("stop_area:IDFM:71370") + "&when=08:30")
opts = plan["options"]
check("hay opciones", len(opts) >= 1, len(opts))
for o in opts:
    print(f'     {o["minutes"]:>3} min  {o["transfers"]} transb  '
          f'{o["walk_minutes"]:>2} min andando  '
          + " -> ".join(f'{x["line_mode"]} {x["line_code"]}' for x in o["legs"]))
check("todas tienen al menos un tramo en transporte publico",
      all(o["legs"] for o in opts))
check("todos los tramos traen linea e id de parada",
      all(x["line_id"].startswith("line:IDFM:") and x["from_id"].startswith("stop_area:IDFM:")
          for o in opts for x in o["legs"]))
check("estan ordenadas de mas rapida a mas lenta",
      [o["minutes"] for o in opts] == sorted(o["minutes"] for o in opts))

print("\n=== guardar la opcion elegida como ruta ===")
r = post("/api/routes/from-plan", {
    "option": opts[0],
    "meta": {"name": "PRUEBA planificador", "origin_name": addr["name"],
             "dest_name": "Gare Saint-Lazare", "days": [0, 1, 2, 3, 4],
             "time_from": "08:00", "time_to": "09:30"},
})
rid = r["id"]
ruta = r["route"]
check("la ruta se ha creado", bool(rid))
check("tiene tantos tramos como la opcion",
      len(ruta["legs"]) == len(opts[0]["legs"]),
      f'{len(ruta["legs"])} vs {len(opts[0]["legs"])}')
for leg in ruta["legs"]:
    print(f'     {leg["line_mode"]:<8} {leg["line_code"]:<4} desde {leg["from_name"][:24]:<24} '
          f'sentido={leg["directions"] or "(todos)"}')
check("cada tramo tiene linea y parada",
      all(leg["line_id"] and leg["from_id"] for leg in ruta["legs"]))
if r["without_direction"]:
    print("     aviso: sin sentido fijado en", r["without_direction"])

print("\n=== el tablero de esa ruta funciona ===")
b = get(f"/api/board?route_id={rid}&log_history=false")
check("el tablero responde", "legs" in b)
for leg in b["legs"]:
    print(f'     {leg["line_code"]:<4} {leg["status"]["label"]:<12} {len(leg["departures"])} salidas')
    for d in leg["departures"][:2]:
        print(f'        {d["minutes"]:>3} min  {d["destination"][:30]}')
check("algun tramo trae salidas de verdad",
      any(leg["departures"] for leg in b["legs"]),
      "ningun tramo devolvio salidas")

print("\n=== limpieza ===")
req = urllib.request.Request(B + f"/api/routes/{rid}", method="DELETE")
urllib.request.urlopen(req, timeout=20)
check("ruta de prueba borrada", True)

print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLOS: {FAILS}"))
sys.exit(1 if FAILS else 0)
