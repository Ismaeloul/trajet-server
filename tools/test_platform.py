"""Comprueba que la prevision del anden cuenta bien y no se engaña a si misma.

No toca la API: trabaja sobre una base de datos temporal con observaciones
inventadas, para poder afirmar cosas exactas sobre los porcentajes.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone as _tz

UTC = _tz.utc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["TRAJET_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from app import db, platform            # noqa: E402
from app import collector as C                  # noqa: E402
from app.collector import targets, _in_window   # noqa: E402
from app.config import settings         # noqa: E402

FAILS = []
STOP = "stop_area:IDFM:71370"
LINE = "line:IDFM:C01739"


def check(name, cond, extra=""):
    print(f"  {'ok ' if cond else 'MAL'} {name}" + ("" if cond else f"   <<< {extra}"))
    if not cond:
        FAILS.append(name)


def dia(n):
    """n dias antes de hoy, a las 08:12 hora de Paris."""
    base = datetime.now(settings.tz).replace(hour=8, minute=12, second=0,
                                             microsecond=0)
    return base - timedelta(days=n)


def misma_semana(n):
    """n semanas antes: mismo dia de la semana que hoy.

    Los niveles 2 y 3 separan laborable de fin de semana a proposito, asi que
    para probarlos hay que usar dias del mismo tipo. Ademas es el caso real:
    el mismo tren, el mismo dia de la semana."""
    return dia(7 * n)


def main():
    db.init()

    print("=== sin datos no se inventa nada ===")
    check("sin observaciones no hay prevision",
          platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12") is None)

    print("\n=== una sola vez tampoco basta ===")
    platform.record(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12", "21",
                    when=dia(1))
    g = platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12")
    check(f"con 1 observacion sigue callado (minimo {platform.MIN_SAMPLES})",
          g is None, g)

    print("\n=== el mismo tren, varios dias ===")
    for n in (2, 3, 4):
        platform.record(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12", "21",
                        when=dia(n))
    g = platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12")
    check("ahora si predice", g is not None)
    check("y dice la via 21", g and g["platform"] == "21", g)
    check("con el 100 %", g and g["share"] == 1.0, g)
    check("y dice que es por el numero de tren", g and g["basis"] == "mision", g)

    print("\n=== el mismo dia repetido no cuenta cuatro veces ===")
    antes = platform.accuracy()["observations"]
    for _ in range(5):
        platform.record(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12", "21",
                        when=dia(2))
    check("refrescar la pantalla no infla el historico",
          platform.accuracy()["observations"] == antes,
          f'{platform.accuracy()["observations"]} vs {antes}')

    print("\n=== si el anden cambia, baja la confianza ===")
    for n in (5, 6):
        platform.record(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12", "17",
                        when=dia(n))
    g = platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12")
    check("sigue diciendo la 21, que es la mayoritaria",
          g and g["platform"] == "21", g)
    check("pero ya no al 100 %", g and g["share"] < 1.0, g)
    check("y cuenta las 6 observaciones", g and g["samples"] == 6, g)

    print("\n=== sin mayoria clara, se calla ===")
    for n in (7, 8):
        platform.record(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12", "17",
                        when=dia(n))
    g = platform.predict(STOP, LINE, "Mantes-la-Jolie", "135711", "08:12")
    check("con 4 y 4 no arriesga", g is None, g)

    print("\n=== sin numero de tren, cae a la hora teorica ===")
    for n in range(1, 5):
        platform.record(STOP, LINE, "Rouen Rive Droite", None, "09:40", "23",
                        when=misma_semana(n))
    g = platform.predict(STOP, LINE, "Rouen Rive Droite", None, "09:40")
    check("predice igualmente", g is not None)
    check("y dice que es por la hora", g and g["basis"] == "hora", g)

    print("\n=== los acentos y espacios raros no rompen el emparejado ===")
    g = platform.predict(STOP, LINE, "Rouen Rive Droite", None, "09:40")
    check("mismo destino con espacios finos y duros",
          g and g["platform"] == "23", g)
    g = platform.predict(STOP, LINE, "ROUEN RIVE DROITE", None, "09:40")
    check("y en mayusculas", g and g["platform"] == "23", g)

    print("\n=== laborable y fin de semana no se mezclan ===")
    hoy = datetime.now(settings.tz)
    otro = 0 if hoy.weekday() >= 5 else 5
    g = platform.predict(STOP, LINE, "Rouen Rive Droite", None, "09:40",
                         weekday=otro)
    check("el otro tipo de dia no hereda el historico", g is None, g)

    print("\n=== puntuar sin hacer trampa ===")
    hoy_iso = datetime.now(settings.tz).date().isoformat()
    for n in (1, 2, 3):
        platform.record(STOP, LINE, "Le Havre", "999", "10:00", "5",
                        when=misma_semana(n))
    platform.record(STOP, LINE, "Le Havre", "999", "10:00", "9", when=dia(0))
    con_hoy = platform.predict(STOP, LINE, "Le Havre", "999", "10:00")
    sin_hoy = platform.predict(STOP, LINE, "Le Havre", "999", "10:00",
                               exclude_day=hoy_iso)
    check("con los datos de hoy ve 4 observaciones",
          con_hoy and con_hoy["samples"] == 4, con_hoy)
    check("excluyendo hoy solo ve 3", sin_hoy and sin_hoy["samples"] == 3, sin_hoy)
    check("y la prevision limpia dice la 5",
          sin_hoy and sin_hoy["platform"] == "5", sin_hoy)

    platform.score(STOP, LINE, sin_hoy["platform"], "9")
    acc = platform.accuracy()
    check("el fallo se apunta como fallo", acc["hits"] == 0, acc)
    check("y cuenta como una prevision", acc["predictions"] == 1, acc)

    print("\n=== el recolector: que estaciones estudia ===")
    lunes_8 = datetime.now(settings.tz).replace(hour=8, minute=0)
    while lunes_8.weekday() != 0:
        lunes_8 += timedelta(days=1)
    ruta = {"days": [0, 1, 2, 3, 4], "time_from": "07:00", "time_to": "10:00",
            "legs": [
                {"from_id": STOP, "line_id": LINE,
                 "line_mode": "Train Transilien"},
                {"from_id": "stop_area:IDFM:71264",
                 "line_id": "line:IDFM:C01383", "line_mode": "Métro"},
            ]}
    t = targets([ruta])
    check("solo sondea la estacion del tren", list(t) == [STOP], list(t))
    check("el metro se descarta: no publica anden nunca", len(t) == 1, t)

    print("\n=== la franja ya no decide SI se aprende, solo el ritmo ===")
    check("dentro de la franja, prioridad", _in_window(ruta, lunes_8))
    check("fuera de la franja, sin prioridad",
          not _in_window(ruta, lunes_8.replace(hour=14)))
    check("pero fuera de la franja se sigue estudiando esa estacion",
          list(targets([ruta])) == [STOP])

    print("\n=== el ritmo se adapta a la cuota que queda ===")
    tarde = lunes_8.replace(hour=14)
    # Las cifras salen de la RESERVA, no fijas: si se cambia la reserva el
    # test se ajusta solo en vez de fallar por algo que no es un fallo.
    mucho = C.RESERVA + 600
    poco = C.RESERVA + 60
    i_mucho, _ = C.plan_interval(mucho, 1, tarde, priority=False)
    i_poco, _ = C.plan_interval(poco, 1, tarde, priority=False)
    i_pelado, motivo = C.plan_interval(C.RESERVA, 1, tarde, priority=False)
    check("con mucha cuota, muestrea mas a menudo que con poca",
          i_mucho <= i_poco, f"{i_mucho:.0f} vs {i_poco:.0f}")
    check("agotada la reserva, se calla", i_pelado == 0, motivo)
    check("y explica por que", "cuota agotada" in motivo, motivo)
    check("nunca baja del minimo", i_mucho >= C.MIN_INTERVAL, i_mucho)
    check("ni sube del maximo", i_poco <= C.MAX_INTERVAL, i_poco)

    # Con cuota de sobra los dos llegan al suelo de MIN_INTERVAL y la
    # prioridad ya no puede bajar mas, asi que se prueba con presupuesto justo,
    # que es cuando la prioridad de verdad decide algo.
    i_pri, _ = C.plan_interval(poco, 1, lunes_8, priority=True)
    i_no, _ = C.plan_interval(poco, 1, lunes_8, priority=False)
    check("en la franja de una ruta mira mas a menudo",
          i_pri < i_no, f"{i_pri:.0f} vs {i_no:.0f}")
    check("y con cuota de sobra los dos van al minimo",
          C.plan_interval(mucho, 1, lunes_8, priority=True)[0] == C.MIN_INTERVAL)

    i_dos, _ = C.plan_interval(mucho, 2, tarde, priority=False)
    i_uno, _ = C.plan_interval(mucho, 1, tarde, priority=False)
    check("con dos estaciones cada pasada cuesta el doble, asi que espacia mas",
          i_dos >= i_uno, f"{i_dos:.0f} vs {i_uno:.0f}")

    i_noche, motivo_n = C.plan_interval(mucho, 1, tarde.replace(hour=3),
                                        priority=False)
    check("de madrugada no gasta nada", i_noche == 0, motivo_n)
    i_sin, motivo_s = C.plan_interval(mucho, 0, tarde, priority=False)
    check("sin rutas de tren, tampoco", i_sin == 0, motivo_s)

    i_nueva, motivo_x = C.plan_interval(None, 1, tarde, priority=False)
    check("si aun no sabe la cuota, va prudente y la aprende de la cabecera",
          i_nueva > 0 and "desconocida" in motivo_x, motivo_x)

    print()
    print("=== no estudia mas estaciones de la cuenta ===")
    # Con seis rutas guardadas salian nueve estaciones y el aprendizaje se
    # comia la cuota que necesita la pantalla.
    muchas = [{"id": i, "days": [0, 1, 2, 3, 4],
               "time_from": "07:00", "time_to": "10:00",
               "legs": [{"from_id": f"stop_area:IDFM:{7000 + i}",
                         "line_id": LINE, "line_mode": "Train Transilien"}]}
              for i in range(9)]
    check(f"nueve rutas -> como mucho {C.MAX_ESTACIONES} estaciones",
          len(targets(muchas)) == C.MAX_ESTACIONES, len(targets(muchas)))

    print("\n=== reparte lo que queda hasta el reinicio de medianoche UTC ===")
    faltan = C.seconds_to_reset(datetime(2026, 8, 31, 18, 0, tzinfo=UTC))
    check("a las 18:00 UTC quedan 6 h", abs(faltan - 6 * 3600) < 2, faltan)
    faltan2 = C.seconds_to_reset(datetime(2026, 8, 31, 23, 30, tzinfo=UTC))
    check("a las 23:30 UTC queda media hora", abs(faltan2 - 1800) < 2, faltan2)

    print("\n" + ("TODO OK" if not FAILS else f"{len(FAILS)} FALLOS: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
