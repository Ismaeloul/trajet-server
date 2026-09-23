"""La API REAL de PRIM: la clave, los tres endpoints y el guardado cifrado.

Unas 16 llamadas en total (7 stop-monitoring, 4 general-message, 5 navitia).
La primera pasada (24/09/2026, 01:33 en Paris) gasto 24: 15 de la tanda, 1
para ver que /coverage no existe en PRIM y 8 al repetir las 2 que fallaban.
Cada respuesta buena se guarda recortada en tests/fixtures/prim/ (sin la
clave ni las cabeceras) como caso nuevo para el PRIM falso, y el README de
esa carpeta dice de donde sale y cuando.

Regla de estas pruebas: la clave NUNCA entra en un assert ni en un mensaje.
Lo que haya que comprobar sobre ella se calcula antes y se afirma el booleano.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from app.idfm import lines_in_message

pytestmark = pytest.mark.real

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "prim")
LEEME = os.path.join(FIXTURES, "README.md")

SAINT_LAZARE = "STIF:StopArea:SP:71370:"
ARGENTEUIL = "STIF:StopArea:SP:65063:"
MAX_VISITAS = 40
MAX_AVISOS = 40
MAX_LINEAS = 3
MAX_PERTURBACIONES = 5
# Lineas que se quedan primero al recortar: las de la ruta de casa.
PREFERIDAS = ("C01739", "C01383", "C01384", "C01743")
CLAVE_FALSA = "clave0falsa0de0las0pruebas0reales"


def _contiene(texto: str, secreto: str) -> bool:
    return bool(secreto) and secreto in texto


# ---------------- recortes y guardado ----------------

def _recortar_visitas(data: dict) -> tuple[dict, str]:
    entregas = data["Siri"]["ServiceDelivery"]["StopMonitoringDelivery"]
    todas = [v for e in entregas for v in (e.get("MonitoredStopVisit") or [])]

    def linea(v):
        ref = ((v.get("MonitoredVehicleJourney") or {}).get("LineRef") or {}).get("value", "")
        return ref.rstrip(":").rsplit(":", 1)[-1]

    buenas = [v for v in todas if linea(v) in PREFERIDAS][: MAX_VISITAS // 2]
    resto = [v for v in todas if v not in buenas]
    quedan = buenas + resto[: MAX_VISITAS - len(buenas)]
    orden = {id(v): i for i, v in enumerate(todas)}
    quedan.sort(key=lambda v: orden[id(v)])
    for e in entregas:
        e["MonitoredStopVisit"] = []
    if entregas:
        entregas[0]["MonitoredStopVisit"] = quedan
    return data, f"{len(quedan)} de {len(todas)} visitas (primero J, 13, 14 y E)"


def _recortar_avisos(data: dict) -> tuple[dict, str]:
    entregas = data["Siri"]["ServiceDelivery"]["GeneralMessageDelivery"]
    todos = [m for e in entregas for m in (e.get("InfoMessage") or [])]
    buenos = [m for m in todos if lines_in_message(m) & set(PREFERIDAS)]
    resto = [m for m in todos if m not in buenos]
    quedan = (buenos + resto)[:MAX_AVISOS]
    for e in entregas:
        e["InfoMessage"] = []
    if entregas:
        entregas[0]["InfoMessage"] = quedan
    return data, f"{len(quedan)} de {len(todos)} avisos (primero los de J, 13, 14 y E)"


def _aligerar(node):
    """Navitia repite en cada parada la lista entera de lineas que pasan por
    ella (Saint-Lazare, 38), mete el trazado de cada tramo y cada paso de las
    instrucciones a pie: es casi todo el peso. Se quitan los trazados y se
    dejan 3 lineas por parada, las 2 primeras y 2 ultimas paradas de cada
    tramo y 5 instrucciones a pie. El tablero y el planificador no usan nada
    de eso."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "geojson":
                continue
            if isinstance(v, list):
                if k == "lines":
                    v = v[:MAX_LINEAS]
                elif k == "stop_date_times" and len(v) > 4:
                    v = v[:2] + v[-2:]
                elif k == "path":
                    v = v[:5]
            out[k] = _aligerar(v)
        return out
    if isinstance(node, list):
        return [_aligerar(v) for v in node]
    return node


def _recortar_itinerarios(data: dict) -> tuple[dict, str]:
    total = len(data.get("journeys") or [])
    perturbaciones = len(data.get("disruptions") or [])
    data = _aligerar(data)
    data["journeys"] = (data.get("journeys") or [])[:3]
    data["disruptions"] = (data.get("disruptions") or [])[:MAX_PERTURBACIONES]
    return data, (f"{len(data['journeys'])} de {total} itinerarios y "
                  f"{len(data['disruptions'])} de {perturbaciones} perturbaciones; "
                  "sin trazados, 3 lineas por parada y 4 paradas por tramo")


def _apuntar_en_leeme(nombre: str, peticion: str, recorte: str) -> None:
    filas: dict[str, str] = {}
    if os.path.exists(LEEME):
        with open(LEEME, encoding="utf-8") as f:
            for linea in f:
                if linea.startswith("| `"):
                    filas[linea.split("`")[1]] = linea.rstrip("\n")
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    filas[nombre] = f"| `{nombre}` | `{peticion}` | {ahora} | {recorte} |"
    cabecera = [
        "# Respuestas reales de PRIM (para el PRIM falso y los tests sin red)",
        "",
        "Capturadas por `tests/real/test_prim_real.py` (se lanzan con",
        "`scripts/test-real.sh`) contra `https://prim.iledefrance-mobilites.fr`,",
        "con la clave del servidor. Se guarda SOLO el cuerpo JSON de la respuesta:",
        "ni la clave ni ninguna cabecera (tampoco las de la peticion). Antes de",
        "escribir cada fichero se comprueba que la clave no aparece en el texto.",
        "",
        "Recortes (para que el repo no cargue con cientos de KB): como mucho 40",
        "visitas por estacion y 40 avisos, dando prioridad a las lineas de la ruta",
        "de casa (J `C01739`, metro 13 `C01383`, metro 14 `C01384`, RER E `C01743`).",
        "En Navitia se quitan los trazados (`geojson`) y se dejan 3 lineas en cada",
        "parada (`lines`), las 2 primeras y 2 ultimas paradas de cada tramo",
        "(`stop_date_times`), 5 instrucciones a pie (`path`), 3 itinerarios y 5",
        "perturbaciones. Nada mas se toca: el resto es la respuesta tal cual.",
        "",
        "Las horas son las del momento de la captura. `tests/test_prim.py` las",
        "mueve al presente antes de pasarlas por el tablero.",
        "",
        "| fichero | peticion | capturado | recorte |",
        "|---|---|---|---|",
    ]
    os.makedirs(FIXTURES, exist_ok=True)
    with open(LEEME, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(cabecera + [filas[k] for k in sorted(filas)]) + "\n")


def _guardar(nombre: str, data: dict, clave: str, peticion: str, recorte: str) -> None:
    texto = json.dumps(data, ensure_ascii=False, indent=1)
    fuga = _contiene(texto, clave)
    assert not fuga, f"la clave aparece en la respuesta de {nombre}: no se guarda"
    os.makedirs(FIXTURES, exist_ok=True)
    with open(os.path.join(FIXTURES, nombre), "w", encoding="utf-8", newline="\n") as f:
        f.write(texto + "\n")
    _apuntar_en_leeme(nombre, peticion, recorte)


# ---------------- la clave ----------------

async def test_validate_key_con_la_clave_real(servidor, clave_real):
    c = await servidor.arrancar(clave_real)
    checks = await c.validate_key(clave_real)
    resumen = [(ch["api"], ch["status"], ch["message"]) for ch in checks]
    assert all(ch["ok"] for ch in checks), resumen
    assert servidor.transporte.hechas.count("stop-monitoring") == 1
    assert len(servidor.transporte.hechas) == 3
    # No cuentan como trafico de la app, pero lo que dice PRIM se apunta.
    snap = c.quota_counter.snapshot()
    assert all(e["used"] == 0 for e in snap["endpoints"])
    assert all(e["remaining_reported"] is not None for e in snap["endpoints"]), snap


async def test_validate_key_con_una_clave_falsa(servidor, clave_real):
    c = await servidor.arrancar(clave_real)
    checks = await c.validate_key(CLAVE_FALSA)
    resumen = [(ch["api"], ch["status"], ch["message"]) for ch in checks]
    assert all(ch["status"] == 401 and not ch["ok"] for ch in checks), resumen
    assert all("no válida" in ch["message"] for ch in checks)


# ---------------- SIRI ----------------

async def test_stop_monitoring_saint_lazare(servidor, clave_real):
    c = await servidor.arrancar(clave_real)
    data, age = await c.stop_monitoring(SAINT_LAZARE)
    assert age == 0.0 and c.last_error is None
    assert "StopMonitoringDelivery" in data["Siri"]["ServiceDelivery"]
    assert c.quota_counter.used("stop-monitoring") == 1
    data, recorte = _recortar_visitas(data)
    _guardar("stop-monitoring-saint-lazare.json", data, clave_real,
             f"GET /marketplace/stop-monitoring?MonitoringRef={SAINT_LAZARE}", recorte)


async def test_stop_monitoring_argenteuil(servidor, clave_real):
    c = await servidor.arrancar(clave_real)
    data, _ = await c.stop_monitoring(ARGENTEUIL)
    assert "StopMonitoringDelivery" in data["Siri"]["ServiceDelivery"]
    data, recorte = _recortar_visitas(data)
    _guardar("stop-monitoring-argenteuil.json", data, clave_real,
             f"GET /marketplace/stop-monitoring?MonitoringRef={ARGENTEUIL}", recorte)


async def test_general_message(servidor, clave_real):
    from app import board
    c = await servidor.arrancar(clave_real)
    data, _ = await c.general_message()
    avisos = board.index_disruptions(data)
    assert avisos, "general-message sin ningun aviso activo en toda la red"
    data, recorte = _recortar_avisos(data)
    _guardar("general-message.json", data, clave_real,
             "GET /marketplace/general-message?LineRef=ALL", recorte)


# ---------------- Navitia ----------------

async def test_navitia_places_argenteuil(servidor, clave_real):
    c = await servidor.arrancar(clave_real)
    data, _ = await c.places("argenteuil")
    ids = [(p.get("stop_area") or {}).get("id") for p in data.get("places", [])]
    assert "stop_area:IDFM:65063" in ids, ids
    data = _aligerar(data)
    _guardar("navitia-places-argenteuil.json", data, clave_real,
             "GET /marketplace/v2/navitia/places?q=argenteuil&count=12&type[]=stop_area",
             f"{len(data.get('places', []))} sitios; 3 lineas por parada")


async def test_navitia_journeys_saint_lazare_argenteuil(servidor, clave_real):
    from app import planner
    c = await servidor.arrancar(clave_real)
    data, _ = await c.journeys("stop_area:IDFM:71370", "stop_area:IDFM:65063")
    journeys = data.get("journeys") or []
    assert journeys, data.get("error")
    opciones = planner.parse_journeys(data)
    assert opciones, "ningun itinerario en transporte publico"
    data, recorte = _recortar_itinerarios(data)
    _guardar("navitia-journeys-saint-lazare-argenteuil.json", data, clave_real,
             "GET /marketplace/v2/navitia/journeys?from=stop_area:IDFM:71370"
             "&to=stop_area:IDFM:65063&min_nb_journeys=3&max_nb_journeys=5"
             "&data_freshness=realtime", recorte)


# ---------------- guardado cifrado, reemplazo en caliente y borrado ----------------

async def test_guardado_cifrado_reemplazo_en_caliente_y_borrado(servidor, clave_real):
    """Arranca con una clave de entorno que PRIM rechaza, guarda la real
    desde el «panel», la usa sin reiniciar y al borrarla vuelve a la del
    entorno. 5 llamadas."""
    from app import prim
    from app.prim import PrimError

    c = await servidor.arrancar(CLAVE_FALSA)
    with pytest.raises(PrimError) as e:
        await c.stop_monitoring(SAINT_LAZARE)
    assert e.value.kind == "invalid" and c.key_state() == "invalid"

    res = await prim.set_key_and_save(clave_real)
    assert res["saved"] is True, res["checks"]
    info = res["info"]
    assert info["source"] == "panel" and info["state"] == "valid"
    assert info["encryption"] == "app_seed"
    ultimos = info["last4"] == clave_real[-4:]
    assert ultimos
    en_uso = c._key == clave_real
    assert en_uso

    fichero = os.path.join(servidor.data, "secrets", "prim-key.json")
    with open(fichero, encoding="utf-8") as f:
        crudo = f.read()
    fuga = _contiene(crudo, clave_real)
    assert not fuga

    # Sin reiniciar: la pausa del 401 ya no cuenta y la cache esta vacia.
    data, age = await c.stop_monitoring(SAINT_LAZARE)
    assert age == 0.0 and "Siri" in data and c.last_error is None

    info = await prim.delete_saved_key()
    assert info["source"] == "env" and not os.path.exists(fichero)
    vuelve = c._key == CLAVE_FALSA
    assert vuelve
    # En toda la prueba, ni una respuesta de la API lleva la clave.
    todo = json.dumps([res, info, prim.prim_state(), prim.server_state()])
    fuga = _contiene(todo, clave_real)
    assert not fuga
    assert len(servidor.transporte.hechas) == 5
