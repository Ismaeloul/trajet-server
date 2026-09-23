"""Planificar por hora de llegada, de extremo a extremo contra la API real."""
import json
import sys
import urllib.parse
import urllib.request

B = "http://localhost:7796"
FAILS = []


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'MAL'} {name}" + ("" if cond else f"   <<< {extra}"))
    if not cond:
        FAILS.append(name)


def get(p):
    return json.load(urllib.request.urlopen(B + p, timeout=45))


def post(p, body):
    req = urllib.request.Request(
        B + p, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=60))


ADDR = "2.35995;48.855602"          # 12 Rue de Rivoli
DEST = "stop_area:IDFM:71370"       # Gare Saint-Lazare
Q = ("?from=" + urllib.parse.quote(ADDR) + "&to=" + urllib.parse.quote(DEST))

print("=== salir a las 09:00 ===")
sal = get(Q.join(["/api/plan", ""]) + "&when=09:00&mode=departure")["options"]
for o in sal:
    print(f'     sale {o["departure"]} llega {o["arrival"]}  {o["minutes"]:>3} min  '
          + " -> ".join(f'{x["line_mode"]} {x["line_code"]}' for x in o["legs"]))
check("todas salen a las 09:00 o despues",
      all(o["departure"] >= "09:00" for o in sal), [o["departure"] for o in sal])
check("la primera es la mas rapida",
      sal[0]["minutes"] == min(o["minutes"] for o in sal))

print("\n=== llegar a las 09:00 ===")
lle = get(Q.join(["/api/plan", ""]) + "&when=09:00&mode=arrival")["options"]
for o in lle:
    print(f'     sale {o["departure"]} llega {o["arrival"]}  {o["minutes"]:>3} min  '
          + " -> ".join(f'{x["line_mode"]} {x["line_code"]}' for x in o["legs"]))
check("todas llegan a las 09:00 o antes",
      all(o["arrival"] <= "09:00" for o in lle), [o["arrival"] for o in lle])
check("la primera es la que te deja salir mas tarde",
      lle[0]["departure"] == max(o["departure"] for o in lle),
      [o["departure"] for o in lle])
check("son itinerarios distintos a los de salida",
      lle[0]["departure"] != sal[0]["departure"])

print("\n=== un modo invalido se rechaza ===")
try:
    get(Q.join(["/api/plan", ""]) + "&when=09:00&mode=cualquiera")
    check("mode invalido da error", False, "no dio error")
except urllib.error.HTTPError as e:
    check("mode invalido da error 400", e.code == 400, e.code)

print("\n=== guardar como 'llego a las 09:00' ===")
r = post("/api/routes/from-plan", {
    "option": lle[0],
    "meta": {"name": "PRUEBA llegada", "origin_name": "12 Rue de Rivoli",
             "dest_name": "Gare Saint-Lazare", "days": [0, 1, 2, 3, 4],
             "time_mode": "arrival", "time_at": "09:00"},
})
ruta = r["route"]
dur = lle[0]["minutes"]
print(f'     modo={ruta["time_mode"]} hora={ruta["time_at"]} '
      f'duracion={ruta["duration_min"]} franja={ruta["time_from"]}-{ruta["time_to"]}')
check("guarda el modo llegada", ruta["time_mode"] == "arrival")
check("guarda la hora", ruta["time_at"] == "09:00")
check("guarda la duracion real del itinerario elegido",
      ruta["duration_min"] == dur, f'{ruta["duration_min"]} vs {dur}')
check("la franja termina despues de la hora de llegada",
      ruta["time_to"] >= "09:00", ruta["time_to"])
check("y empieza con tiempo de sobra antes",
      ruta["time_from"] < ruta["time_at"], ruta["time_from"])

print("\n=== el tablero de esa ruta funciona ===")
b = get(f'/api/board?route_id={ruta["id"]}&log_history=false')
check("responde", "legs" in b)
for leg in b["legs"]:
    print(f'     {leg["line_code"]:<4} {leg["status"]["label"]:<12} '
          f'{len(leg["departures"])} salidas')
check("trae salidas", any(l["departures"] for l in b["legs"]))

urllib.request.urlopen(urllib.request.Request(
    B + f'/api/routes/{ruta["id"]}', method="DELETE"), timeout=20)
print("\n     ruta de prueba borrada")

print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLOS: {FAILS}"))
sys.exit(1 if FAILS else 0)
