# Muestras reales de datos abiertos de IDFM (para los tests del mapa)

Descargadas el **24/09/2026 a las 00:36 (hora de París)** = 2026-09-23T22:36Z,
**sin ninguna clave**, con peticiones filtradas (`where`/`select`) a la API
Explore v2.1 de Opendatasoft de `data.iledefrance-mobilites.fr`, salvo los
`gtfs-*.txt`, que son filas del GTFS oficial. Nada está inventado ni retocado:
los JSON/GeoJSON son la respuesta tal cual (solo re-serializada con sangría o
comprimida con gzip), y los `.txt` conservan la cabecera y las filas originales.

Total: unos 177 KB de datos (184 KB con este README). Explicación completa de cada dataset, de los
identificadores y del diseño del módulo de mapa: `docs/datos-idfm.md` (repo
`trajet-ios`).

Base de las URL: `https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets/`

## Qué hay en cada fichero

| fichero | dataset (id Opendatasoft) | petición (`exports/…?`) | registros | licencia |
|---|---|---|---|---|
| `referentiel-lignes-J.json` | `referentiel-des-lignes` | `json` · `where=id_line='C01739'` | 1 | ODbL |
| `referentiel-lignes-14-272.json` | `referentiel-des-lignes` | `json` · `where=id_line in ('C01384','C01254')` | 2 | ODbL |
| `traces-ferre-J-saint-lazare-argenteuil.geojson` | `traces-du-reseau-ferre-idf` | `geojson` · `where=(idrefligc='C01739' or res_com='TRAIN J') and intersects(geo_shape, geom'POLYGON((2.25 48.87, 2.33 48.87, 2.33 48.951, 2.25 48.951, 2.25 48.87))')` · `select=objectid_1,idrefligc,indice_lig,res_com,mode,shape_leng,colourweb_hexa` | 8 tramos | Licence Ouverte 2.0 |
| `traces-gtfs-ligne-J.geojson.gz` | `traces-des-lignes-de-transport-en-commun-idfm` | `geojson` · `where=id_ilico='C01739'` · `select=route_id,id_ilico,route_short_name,route_type,route_color,operatorname,networkname,shape` | 1 (MultiLineString, 23 partes) | ODbL (trazados calculados sobre OpenStreetMap) |
| `traces-gtfs-metro-14.geojson.gz` | ídem | ídem con `id_ilico='C01384'` | 1 (19 partes) | ODbL |
| `traces-gtfs-bus-272.geojson.gz` | ídem | ídem con `id_ilico='C01254'` | 1 (13 partes) | ODbL |
| `gares-saint-lazare-argenteuil.geojson` | `emplacement-des-gares-idf` | `geojson` · `where=id_ref_zdc in (71370, 65063)` · `select=id_gares,nom_gares,nom_iv,id_ref_zdc,id_ref_zda,idrefligc,res_com,indice_lig,mode` | 7 | Licence Ouverte 2.0 |
| `zdc-saint-lazare-argenteuil.json` | `zones-de-correspondance` | `json` · `where=zdcid in ('71370','65063')` | 2 | Licence Ouverte 2.0 |
| `zda-saint-lazare-argenteuil.json` | `zones-d-arrets` | `json` · `where=zdcid in ('71370','65063')` | 11 | Licence Ouverte 2.0 |
| `arrets-saint-lazare.geojson` | `arrets` | `geojson` · `where=zdaid in (<las 8 ZdA de la ZdC 71370>)` · `select=arrid,arrname,arrtype,zdaid,arrtown,arraccessibility,arrgeopoint` | 55 | Licence Ouverte 2.0 |
| `arrets-argenteuil.geojson` | `arrets` | ídem con las 3 ZdA de la ZdC 65063 | 24 | Licence Ouverte 2.0 |
| `relations-acces-saint-lazare-argenteuil.json` | `relations-acces` | `json` · `where=zdaid in (<las 11 ZdA>)` | 22 | Licence Ouverte 2.0 |
| `acces-saint-lazare.geojson` | `acces` | `geojson` · `where=accid in (<los 14 accid de relations-acces>)` · `select=accid,accname,accshortname,accdescription,accisentry,accisexit,fournisseurname,accgeopoint` | 14 | Licence Ouverte 2.0 |
| `acces-argenteuil.geojson` | `acces` | ídem con los 4 accid de Argenteuil | 4 | Licence Ouverte 2.0 |
| `arrets-lignes-J.json` | `arrets-lignes` | `json` · `where=id='IDFM:C01739'` · `select=id,route_long_name,stop_id,stop_name,stop_lon,stop_lat,mode,nom_commune` | 54 | ODbL |
| `arrets-transporteur-vias-saint-lazare.json` | `arrets-transporteur` | `json` · `where=arrid in (<los 27 arrêts rail de la ZdA 58566>)` · `select=artid,arrid,artname,fournisseurname,arttype,privatecode,publiccode,artgeopoint` | 28 | Licence Ouverte 2.0 |
| `catalogo-frescura.json` | (catálogo) | `…/catalog/datasets?where=dataset_id in (…12 ids…)&select=dataset_id,modified,data_processed,records_count,license` | 12 | — (metadatos) |
| `gtfs-stops-saint-lazare-argenteuil.txt` | GTFS `offre-horaires-tc-gtfs-idfm`, `stops.txt` | filas con `stop_id` o `parent_station` = `IDFM:71370` / `IDFM:65063` | 67 | Licence Mobilités |
| `gtfs-pathways-saint-lazare-argenteuil.txt` | ídem, `pathways.txt` | filas con `from_stop_id` o `to_stop_id` en el conjunto anterior | 117 | Licence Mobilités |
| `gtfs-transfers-saint-lazare-argenteuil.txt` | ídem, `transfers.txt` | filas entre paradas de esas dos zonas en las que uno de los extremos es `IDFM:monomodalStopPlace:58566` o `:47875` | 90 | Licence Mobilités |

Los tres `gtfs-*.txt` salen de `https://eu.ftp.opendatasoft.com/stif/GTFS/IDFM-gtfs.zip`
(versión con `Last-Modified: Wed, 23 Sep 2026 14:23:37 GMT`,
`ETag: "6ab3e0e9-7dda5cc"`), leyendo solo esos tres miembros del zip con
peticiones HTTP `Range`, sin bajar los 132 MB.

Frescura de cada dataset en el momento de la descarga (`data_processed`): ver
`catalogo-frescura.json` (p. ej. trazados GTFS 2026-09-23T15:00:42Z, trazados
ferroviarios 2026-07-16T14:43:15Z, accesos 2026-09-23T00:30:34Z).

## Identificadores que aparecen (para escribir los tests)

- Línea J: `C01739` (referencial) = `IDFM:C01739` (GTFS) = `line:IDFM:C01739` (Navitia) = `STIF:Line::C01739:` (SIRI). Colores `cec73d` / texto `000000`.
- Metro 14: `C01384`, `640082`/`ffffff`. Bus 272: `C01254`, `ff5a00`/`000000`.
- Gare Saint-Lazare: ZdC `71370` (= `stop_area:IDFM:71370` en Navitia); ZdA de los trenes `58566` (= `IDFM:monomodalStopPlace:58566` en el GTFS = `STIF:StopArea:SP:58566:` en SIRI); ZdA del metro `462374`; andén del metro 14 `462972` (= `STIF:StopPoint:Q:462972:`).
- Argenteuil: ZdC `65063`; ZdA de los trenes `47875`.
- Acceso «r. Saint-Lazare» nº 11: `accid 50148706` = `IDFM:StopPlaceEntrance:50148706` en el GTFS.

## Trampas reales que estas muestras cubren

- El tramo `objectid_1=550` de la J (junto a Bois-Colombes, hacia Houilles)
  trae `idrefligc='C0173'` (truncado): filtrando solo por `idrefligc='C01739'`
  se pierde. Por eso la petición añade `or res_com='TRAIN J'`.
- `traces-gtfs-*`: cada línea es un único MultiLineString con un recorrido
  por sentido y variante, solapados (la J suma 982 km para una red de ~180 km).
- En la ZdA 58566 hay casi un arrêt por vía: la vía (1 a 27) está en
  `publiccode` de `arrets-transporteur-vias-saint-lazare.json`, pero las vías
  6 y 7 comparten `arrid 471658` y el `arrid 41194` («GARE ST LAZARE») trae
  `publiccode='-'`.
- `pathways.txt` solo une accesos con paradas (siempre `pathway_mode=1`), sin
  geometría; no hay caminos andén-andén.

## Licencias y atribución

- **Licence Ouverte v2.0 (Etalab)**: mencionar la fuente (Île-de-France
  Mobilités) y la fecha de última actualización.
- **ODbL (versión francesa)**: aviso de que el contenido procede de la base
  de IDFM bajo ODbL; los trazados GTFS se calculan sobre OpenStreetMap
  («© contributeurs OpenStreetMap»). Obligación de compartir igual solo si se
  usa públicamente una base derivada.
- **Licence Mobilités** (GTFS): basada en la ODbL, con identificación del
  reutilizador y compromiso de uso compatible con la estrategia de movilidad
  de la región. Estas filas son un extracto mínimo para tests en un repo privado.

Detalle y enlaces a los textos: `docs/datos-idfm.md`, sección «Licencias».
