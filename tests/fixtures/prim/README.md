# Respuestas reales de PRIM (para el PRIM falso y los tests sin red)

Capturadas por `tests/real/test_prim_real.py` (se lanzan con
`scripts/test-real.sh`) contra `https://prim.iledefrance-mobilites.fr`,
con la clave del servidor. Se guarda SOLO el cuerpo JSON de la respuesta:
ni la clave ni ninguna cabecera (tampoco las de la peticion). Antes de
escribir cada fichero se comprueba que la clave no aparece en el texto.

Recortes (para que el repo no cargue con cientos de KB): como mucho 40
visitas por estacion y 40 avisos, dando prioridad a las lineas de la ruta
de casa (J `C01739`, metro 13 `C01383`, metro 14 `C01384`, RER E `C01743`).
En Navitia se quitan los trazados (`geojson`) y se dejan 3 lineas en cada
parada (`lines`), las 2 primeras y 2 ultimas paradas de cada tramo
(`stop_date_times`), 5 instrucciones a pie (`path`), 3 itinerarios y 5
perturbaciones. Nada mas se toca: el resto es la respuesta tal cual.

Las horas son las del momento de la captura. `tests/test_prim.py` las
mueve al presente antes de pasarlas por el tablero.

| fichero | peticion | capturado | recorte |
|---|---|---|---|
| `general-message.json` | `GET /marketplace/general-message?LineRef=ALL` | 2026-09-23 23:33 UTC | 40 de 420 avisos (primero los de J, 13, 14 y E) |
| `navitia-journeys-saint-lazare-argenteuil.json` | `GET /marketplace/v2/navitia/journeys?from=stop_area:IDFM:71370&to=stop_area:IDFM:65063&min_nb_journeys=3&max_nb_journeys=5&data_freshness=realtime` | 2026-09-23 23:33 UTC | 3 de 3 itinerarios y 5 de 29 perturbaciones; sin trazados, 3 lineas por parada y 4 paradas por tramo |
| `navitia-places-argenteuil.json` | `GET /marketplace/v2/navitia/places?q=argenteuil&count=12&type[]=stop_area` | 2026-09-23 23:33 UTC | 12 sitios; 3 lineas por parada |
| `stop-monitoring-argenteuil.json` | `GET /marketplace/stop-monitoring?MonitoringRef=STIF:StopArea:SP:65063:` | 2026-09-23 23:33 UTC | 17 de 17 visitas (primero J, 13, 14 y E) |
| `stop-monitoring-saint-lazare.json` | `GET /marketplace/stop-monitoring?MonitoringRef=STIF:StopArea:SP:71370:` | 2026-09-23 23:33 UTC | 40 de 61 visitas (primero J, 13, 14 y E) |
