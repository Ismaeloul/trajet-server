# Trajet · servidor

El servidor de Trajet: habla con PRIM (el tiempo real de Île-de-France
Mobilités), guarda tus rutas, aprende de qué vía sale cada tren, traduce los
avisos y le da todo mascado a la **app del iPhone**. Trae además un **panel**
para lo que no se hace desde el móvil: pegar la clave de PRIM, emparejar el
iPhone con un QR y ver cómo va la cuota del día.

Corre en el Umbrel como la app `ismaeloul-trajet` de la tienda, en el puerto
**7796**. Python 3.12 + FastAPI + httpx + SQLite, en un solo proceso.

La web de la 0.3.0 ya no existe: la app es la del iPhone
([`trajet-ios`](https://github.com/Ismaeloul/trajet-ios); su
[README](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/README.md)
explica cómo se instala, qué IPA usar según cómo se firme y cómo se empareja
con este servidor). Las rutas de la API de siempre (`/api/*`) siguen vivas
por compatibilidad, pero detrás del login de Umbrel y solo desde su proxy: un
cliente viejo sin sesión (la web o una app sin emparejar) deja de ver el
tablero.

---

## Cómo está montado

La documentación del proyecto entero vive en el repo de la app,
[`trajet-ios/docs`](https://github.com/Ismaeloul/trajet-ios/tree/rewrite-v2/docs)
(en este PC, `../docs`):

| documento | qué cuenta |
|---|---|
| [`arquitectura.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/arquitectura.md) | cómo encajan iPhone, Umbrel, PRIM y el panel, con diagramas |
| [`servidor-v2.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/servidor-v2.md) | este servidor por dentro: módulos e interfaces |
| [`openapi.yaml`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/openapi.yaml) | el contrato HTTP, **congelado** (copia en `tests/contract/`) |
| [`reglas.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/reglas.md) | las reglas que no se pueden romper (R63–R90 son del servidor) |
| [`servidor.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/servidor.md) | cómo era la 0.3.0 y por qué cada constante vale lo que vale |
| [`datos-idfm.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/datos-idfm.md) | los datos abiertos del mapa y sus licencias |
| [`fase4-panel.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/fase4-panel.md) | el panel probado en el navegador, con capturas y GIF en `docs/capturas/panel-fase4/` |
| [`pruebas-iphone.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/pruebas-iphone.md) | la lista de comprobación en el iPhone (emparejar con el panel de verdad, revocar, red local y Tailscale) |
| [`PARADAS.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/PARADAS.md) · [`decisiones.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/decisiones.md) | el informe de cada fase y cada decisión con su porqué (las del servidor: Parada 2 y D1.x) |

```
app/
  main.py        la app: middlewares, routers, arranque y parada
  config.py      todo lo configurable, por variables de entorno
  api/           v1.py (iPhone) · admin.py (panel) · legacy.py (0.3.0) · errores, ETag
  auth.py        emparejamiento, tokens y quién puede hablar con el panel
  prim.py        cliente de PRIM: caché, cuota, clave en caliente
  quota.py       contador de cuota por endpoint y día UTC
  keystore.py    la clave de PRIM cifrada en /data/secrets
  logs.py        logs sin secretos y errores recientes para el panel
  mapdata.py     el mapa con datos abiertos de IDFM
  board.py collector.py platform.py planner.py translate.py …   la lógica de siempre
  migrations/    migraciones de SQLite (PRAGMA user_version)
  panel/         el panel: HTML, CSS y JS sin compilar
tests/           pytest con PRIM falso · tests/real contra la API de verdad
scripts/         publish.sh · test-real.sh · sync-contract.sh
tools/ probe/    el sondeo del andén de la 0.3.0 (30/08) y la siembra de la previsión con sus datos
```

**Un solo proceso y un solo worker, a propósito.** La caché de PRIM, el
contador de cuota, el recolector de andenes y los límites del emparejamiento
viven en memoria. Con dos workers habría dos cachés y dos recolectores
gastando el doble de cuota. La imagen arranca con `--workers 1` explícito.

---

## La API

Tres familias de rutas, con el contrato en `docs/openapi.yaml`:

| rutas | para quién | cómo se protege |
|---|---|---|
| `/api/v1/*` | la app del iPhone | fuera del login de Umbrel; **token de dispositivo** obligatorio salvo `/api/v1/ping` y `/api/v1/pair` |
| `/` y `/api/admin/*` | el panel, o sea tú | detrás del login de Umbrel, más la comprobación de origen de abajo |
| `/api/*` | la API de la 0.3.0, por compatibilidad | detrás del login de Umbrel y, como el panel, solo desde el proxy de Umbrel (`TRAJET_ADMIN_PEERS`) |

La v1 responde los errores siempre con el mismo sobre,
`{"error": {"code": "...", "message": "..."}}`, con el mensaje en español;
los GET llevan `ETag` (y un `304` si no ha cambiado nada) y las respuestas
grandes van con gzip. Lo más usado:

| | |
|---|---|
| `GET /api/v1/ping` | ¿hay un Trajet aquí? Sin token, no gasta cuota ni toca la BD |
| `POST /api/v1/pair` | canjea el código del QR por un token |
| `GET /api/v1/board` | el tablero; `server.refresh_hint_s` dice cada cuánto volver a pedirlo |
| `GET /api/v1/routes` · `POST` · `PUT` · `DELETE` | tus rutas |
| `GET /api/v1/routes/{id}/map` | el mapa de una ruta |
| `GET /api/v1/search/places` · `/plan` · `POST /api/v1/routes/from-plan` | el planificador |
| `GET /api/v1/alternatives/{id}` | solo al pedirlas, nunca en el refresco |
| `GET /api/v1/health` · `/stats` · `/platform-model` | estado, cuota, historial y acierto de la vía |

El contrato es la fuente. Si cambia en `trajet-ios`, se copia aquí con
`bash scripts/sync-contract.sh ../docs` y los tests de contrato dicen qué no
cuadra.

---

## Seguridad

- **Login de Umbrel delante de todo** (`PROXY_AUTH_ADD: "true"`) salvo
  `/api/v1/*`, que va en la lista blanca (`PROXY_AUTH_WHITELIST`) porque la
  app del iPhone no tiene sesión de Umbrel. Ahí manda el token.
- **Token de dispositivo**: `trj_` + 256 bits aleatorios. En la BD solo se
  guarda su SHA-256 y se compara en tiempo constante. Cada iPhone emparejado
  sale en el panel y se puede revocar: deja de funcionar al momento.
- **Emparejar**: el código del QR vale 5 minutos y un solo uso. Mal escrito,
  caducado o ya usado dan exactamente la misma respuesta. Como mucho 5 intentos
  por minuto y por IP y 20 cada 5 minutos en total; tras 10 fallos seguidos se
  anulan todos los códigos vivos.
- **El panel comprueba de dónde viene la conexión** (`TRAJET_ADMIN_PEERS=auto`):
  solo acepta lo que llega del proxy de Umbrel (la puerta de enlace de la red
  de Docker) o de `127.0.0.1`. Otra app de la red compartida de Umbrel que
  llamase directamente al contenedor se queda fuera. Lo que modifica pide
  además la cabecera `X-Trajet-Panel: 1` y el mismo origen, así que un
  formulario de otra web no puede tocar nada. La API de la 0.3.0 (`/api/*`)
  pasa por la misma comprobación de conexión (sin la cabecera, que sus
  clientes no mandan): tampoco tiene token y otra app de la red podría
  leer o borrar las rutas.
- **La clave de PRIM nunca sale del servidor.** Se pega en el panel, se prueba
  contra PRIM antes de guardarla y se guarda cifrada con AES-256-GCM, con la
  clave derivada (HKDF) de `APP_SEED`, el secreto que Umbrel da a cada app y
  que vive fuera de `/data`. El fichero (`/data/secrets/prim-key.json`) es
  0600. En claro solo existe en la memoria del proceso: ni en respuestas, ni
  en logs, ni en el HTML, ni en los errores. El panel enseña sus 4 últimos
  caracteres y nada más.
- **Logs sin secretos**: se tachan la clave, los `Bearer …`, los tokens y los
  códigos de emparejamiento, y el log de acceso va sin query strings (así una
  búsqueda de «casa» no deja la dirección en el log).
- **El contenedor**: usuario sin privilegios (uid/gid 1000, el dueño de los
  datos de la app en Umbrel), el código es de solo lectura para el proceso,
  384 MB de memoria y 128 procesos como mucho.

Una decisión que no se ve y conviene saber: la imagen arranca uvicorn con
**`--no-proxy-headers`**. uvicorn las trae encendidas y, si se fía de
`X-Forwarded-For`, cambia la IP de la conexión por la que diga la cabecera.
`auth.py` ya decide por su cuenta de quién fiarse: mira la conexión real para
saber si viene del proxy de Umbrel y solo entonces lee `X-Forwarded-For`. Con
`--proxy-headers --forwarded-allow-ips='*'` el panel vería la IP de tu
navegador en vez de la del proxy (y te daría 403), y cualquier contenedor de
la red entraría al panel mandando `X-Forwarded-For: 127.0.0.1`.
`tests/test_empaquetado.py` lo demuestra con el propio middleware de uvicorn.

---

## Cuota y degradación

PRIM da **1000 llamadas al día por endpoint** (`stop-monitoring`,
`general-message` y `navitia`) y la cuenta vuelve a cero a medianoche UTC.

- El servidor lleva su propio contador por endpoint y día UTC, guardado en
  SQLite, y además lee la cabecera `x-ratelimit-remaining-day` de PRIM; manda
  el más pesimista. Al cambiar de día empieza de cero solo, sin esperar a la
  siguiente llamada.
- Niveles por el peor endpoint: por debajo del **70 %** todo normal; hasta el
  **85 %**, aviso; hasta el **95 %**, crítico; y a partir de ahí, agotada.
- **Degradación suave**: al subir de nivel, el mismo dato se reutiliza más
  tiempo (el doble y luego el cuádruple) y el tablero le dice al iPhone que
  refresque cada 30, 60, 120 o 300 s. Del 95 % en adelante, como mucho una
  llamada cada 10 minutos por dato; en el tope, ninguna.
- **Nunca una pantalla vacía**: si PRIM falla o no se le puede llamar, se
  sirve la última copia con su antigüedad real.
- El recolector de andenes nunca toca las 320 llamadas reservadas a la
  pantalla, no muestrea de 01:00 a 05:00 y solo estudia las estaciones de tus
  rutas. Las alternativas solo se calculan cuando las pides.

La cuota del día se ve en el panel y en la app.

---

## El mapa

Los trazados de las líneas, las paradas y los accesos de las estaciones salen
de los **datos abiertos de Île-de-France Mobilités**
(`data.iledefrance-mobilites.fr`): sin clave y sin gastar ni una llamada de
PRIM. El cliente del portal no manda nunca la cabecera `apikey` de PRIM.

- Se descarga bajo demanda, línea a línea (el conjunto entero no cabe en
  384 MB), se simplifica y se guarda en SQLite. Cada madrugada se mira en el
  catálogo qué ha cambiado y solo se vuelve a bajar eso (los trazados, como
  mucho una vez por semana).
- Si el portal está caído se sirve lo guardado; si no hay nada, las paradas
  sin trazado. `TRAJET_MAP=0` lo apaga y todo lo demás funciona igual.
- **Licencias**: Référentiel des arrêts, accesos y trazados ferroviarios con
  la Licence Ouverte v2.0 (Etalab); trazados de las líneas, referencial de
  líneas y paradas por línea con la ODbL (los trazados se calculan sobre
  OpenStreetMap, © contribuidores de OpenStreetMap); horarios GTFS con la
  Licence Mobilités. Cada respuesta del mapa lleva el texto de atribución
  (`license`) con la fecha de los datos, para que la app lo enseñe. Detalle
  en `docs/datos-idfm.md`.

---

## Desarrollo en este PC

Con el venv del repo (Windows; en Linux o macOS es `.venv/bin/python`):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt

.venv\Scripts\python -m pytest                   # la tanda normal: sin red y sin gastar cuota
.venv\Scripts\python -m ruff check app tests     # lint
```

Los tests usan un PRIM falso (`tests/fakeprim.py`) con los casos de
`PreviewData` y una BD temporal por test: nada sale a la red.

Para ver el servidor andando, con Docker Desktop:

```powershell
docker compose up --build        # panel en http://127.0.0.1:7796
```

Imita al Umbrel: el mismo usuario 1000, 384 MB, `TRAJET_ADMIN_PEERS=auto` y
los datos en `./data`. Escucha solo en `127.0.0.1`. El `.env` es opcional
(mira `.env.example`); sin él arranca sin clave y la pegas en el panel. El
panel funciona porque Docker Desktop entrega las conexiones desde la puerta de
enlace de su red, que es justo lo que `auto` acepta.

### Pruebas contra la API real

```bash
bash scripts/test-real.sh              # o con opciones de pytest: -k places
```

**Gastan cuota de verdad**, unas 16 llamadas por pasada. La clave sale de
`PRIM_API_KEY` o del `.env` y no se imprime nunca. Llevan su propio tope:
**800 llamadas por endpoint y día UTC**, apuntadas en
`.local/real-quota-<día>.json` antes de hacer cada una. La cuota real es de
1000 y el servidor de casa también la usa: llegado el tope, lo que falte se
salta.

### Integración continua

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) corre en cada push y
pull request a `main` y `rewrite-v2` (y a mano con **Run workflow**): ruff,
que el contrato sea OpenAPI válido, pytest sin `tests/real` y que la imagen se
construya y arranque (sin clave, sin red, como uid 1000). No publica nada ni
necesita secretos.

---

## Cómo se probó

- **620 tests** de pytest en verde (2 saltados: los permisos POSIX, que en
  Windows no se pueden comprobar, y la migración contra la copia real de la
  base de datos, que no existe). Van contra un PRIM falso que reproduce los
  casos de `PreviewData` de la app (tramo vacío, línea cortada, aviso sin
  traducir, vía que aparece, vía probable, bus a 106 min, tren en el andén,
  destinos mezclados, errores y timeouts), más los tests de contrato contra
  `openapi.yaml`, cuota, caché, migraciones, emparejamiento (código caducado,
  reutilizado, fuerza bruta), tokens revocados, rutas sin token y que la clave
  no sale nunca (ni en respuestas, ni en logs, ni en el HTML). Cada regla del
  servidor (R63–R90) tiene apuntado el test que la cubre en `docs/reglas.md`.
- **9 pruebas contra la API real** de PRIM (`scripts/test-real.sh`, aparte):
  validar la clave (la real y una falsa), `stop-monitoring` en Saint-Lazare y
  Argenteuil, `general-message`, lugares e itinerarios de Navitia, y la zona
  de la clave del panel de principio a fin (pegar una falsa → rechazada; la
  real → probada y guardada cifrada; reemplazar en caliente; borrar). Llevan
  su propio tope de **800 llamadas por endpoint y día UTC** (unas 16 por
  pasada), y las respuestas reales, recortadas y sin la clave, se guardaron
  como casos nuevos del mock.
- **Verificación independiente** al cerrar la FASE 1: tres verificadores
  (tests y reglas; seguridad con peticiones reales; funcionalidad y contrato
  frente al encargo) encontraron 2 fallos altos —el mismo, visto por dos: la
  API de la 0.3.0 no comprobaba de dónde venía la conexión— y unos 14 medios
  o bajos, todos corregidos con un test que falla antes y pasa después. Un
  cuarto verificador reprodujo los 24 hallazgos: 18 arreglados, 2 parciales
  (arreglados después) y 4 aceptados o fuera de alcance, sin regresiones.
  Detalle en `docs/PARADAS.md` (Parada 2) y `docs/decisiones.md` (D1.8).
- **El panel, en el navegador** (FASE 4): levantado en local con
  `docker compose` y recorrido con clics en móvil (390 × 844) y PC
  (1440 × 900), claro y oscuro, con `curl` haciendo de iPhone: QR con cuenta
  atrás, último minuto, caducado y canjeado; renombrar y revocar (el token
  revocado da 401 al momento); clave falsa rechazada con motivo; cuota, salud,
  Ollama, andenes, errores, ajustes del QR con validación, corte de red y
  recuperación. Cero excepciones de JavaScript, ninguna petición fallida que
  no fuera de una prueba negativa y la clave nunca en el DOM ni en la red.
  Salió una sola corrección, de CSS (las casillas de «Salud» y «Andenes»
  recortaban el valor a 390 px), con su test. Informe y capturas:
  [`docs/fase4-panel.md`](https://github.com/Ismaeloul/trajet-ios/blob/rewrite-v2/docs/fase4-panel.md).
  Lo que no se pudo probar ahí: el login de Umbrel (no hay Umbrel en local) y
  el camino con la clave real por el navegador (cubierto por el test real).

---

## Desplegar en el Umbrel

Esto lo haces tú, por SSH. La imagen se construye **en el propio Umbrel** y se
publica en su registro local (`localhost:5000`, atado a 127.0.0.1): Umbrel
hace un `docker pull` de cada imagen del compose antes de arrancar la app e
ignora `pull_policy`, así que tiene que poder descargarla de algún sitio. Es
el mismo registro que ya usan ipa-station y ace-player-neo.

**1. Entra en el Umbrel**

```bash
ssh umbrel@umbrel.local
```

**2. Trae el código.** La primera vez (el repo es privado: `git clone` te
pedirá tu usuario de GitHub y un token de solo lectura):

```bash
git clone https://github.com/Ismaeloul/trajet-server.git ~/trajet-server
cd ~/trajet-server
git checkout rewrite-v2          # mientras la v2 no esté en main
```

Las siguientes veces: `cd ~/trajet-server && git pull`.

Si prefieres no dejar credenciales en el NAS, mándalo desde el PC:

```powershell
git archive -o trajet-server.tar rewrite-v2
scp trajet-server.tar umbrel@umbrel.local:
ssh umbrel@umbrel.local "rm -rf ~/trajet-server && mkdir ~/trajet-server && tar -xf ~/trajet-server.tar -C ~/trajet-server && rm ~/trajet-server.tar"
```

**3. Construye y publica la imagen**

```bash
sudo bash scripts/publish.sh 0.4.0
```

`sudo` porque el usuario `umbrel` no está en el grupo `docker`. El script
comprueba lo que necesita, arranca el registro local si estaba parado (o lo
crea), construye `localhost:5000/ismaeloul-trajet/trajet:0.4.0`, la **prueba**
(arranca sin red y con una BD vacía, responde a `/api/v1/ping` y corre como
uid 1000) y solo entonces la publica. Si algo falla, lo dice y no publica
nada. `--latest` añade también esa etiqueta; `--help` lo explica todo.

**4. Sube la tienda.** En el PC, en `umbrel-app-store`, la carpeta
`ismaeloul-trajet` con la 0.4.0 (compose y `umbrel-app.yml`) tiene que estar
en `main` en GitHub. Umbrel refresca las tiendas cada poco; cuando lo haga,
la App Store enseñará Trajet 0.4.0.

**5. Instala o actualiza.**

> **Antes**: instala y empareja la app nueva del iPhone. Con la 0.4.0 la web
> desaparece y `/api/*` queda detrás del login de Umbrel, así que hasta tener
> el iPhone emparejado no verías el tablero en ningún sitio.

- **Si ya tenías Trajet instalada desde la tienda**: App Store → Trajet →
  **Actualizar**. Al arrancar, la base de datos se migra sola y deja una copia
  `trajet.db.bak-v0` al lado. Tus rutas, el historial y los andenes
  aprendidos se conservan. Si la migración fallara, la app arranca igual en
  modo degradado (el iPhone recibe 503) y el panel dice por qué.
- **Si vienes del stack de pruebas de `~/trajet`** (el `docker compose` que
  sirve hoy el 7796), hay que pasarle su base de datos a la app:

  ```bash
  # a) parar el stack viejo: libera el 7796 y deja la BD cerrada y entera
  cd ~/trajet && sudo docker compose down

  # b) instalar Trajet desde la App Store (arranca con una BD vacía) y pararla
  umbreld client apps.stop.mutate --appId ismaeloul-trajet   # o desde la interfaz

  # c) cambiar la BD vacía por la de siempre (con sus -wal/-shm si los hay)
  APPDATA=~/umbrel/app-data/ismaeloul-trajet/data
  rm -f $APPDATA/trajet.db $APPDATA/trajet.db-wal $APPDATA/trajet.db-shm
  for f in trajet.db trajet.db-wal trajet.db-shm; do
    [ -e ~/trajet/data/$f ] && cp ~/trajet/data/$f $APPDATA/
  done
  sudo chown -R 1000:1000 $APPDATA    # la 0.3.0 corría como root

  # d) arrancarla: migra la BD y deja trajet.db.bak-v0 al lado
  umbreld client apps.start.mutate --appId ismaeloul-trajet
  ```

  El stack de `~/trajet` se queda donde está, con su BD intacta: es tu vuelta
  atrás más fácil. Cuando la 0.4.0 lleve unos días bien, `rm -rf ~/trajet`.

**6. Comprueba que responde**

```bash
curl -s http://127.0.0.1:7796/api/v1/ping       # {"ok":true,…,"version":"0.4.0",…} sin login
curl -sI http://127.0.0.1:7796/ | head -1         # redirige al login de Umbrel: el panel está protegido
```

**7. Abre el panel** desde el inicio de Umbrel (o `http://umbrel.local:7796`),
con tu login de Umbrel:

- **Clave PRIM**: pégala y guárdala. Trajet la prueba con una llamada a cada
  API de PRIM antes de guardarla; si PRIM la rechaza, te dice cuál y por qué,
  y no guarda nada. Se aplica al momento, sin reiniciar.
- **Ajustes**: las direcciones que irán en el QR, la de casa
  (`http://<IP del NAS en tu red>:7796`) y la de Tailscale
  (`http://<IP de Tailscale del NAS>:7796`). Van aquí y no en la tienda,
  que es pública.
- **Emparejar el iPhone**: genera un QR y escanéalo con la app. El panel dice
  «emparejado» en cuanto el iPhone lo canjea. La primera vez, iOS pide permiso
  para la red local: dáselo o la dirección de casa no funcionará (Tailscale sí).

### Si algo falla

| lo que ves | qué pasa y qué hacer |
|---|---|
| El panel avisa «La base de datos no se pudo migrar al arrancar (…)» y el iPhone recibe 503 | la app arranca en modo degradado para que se vea el motivo. Si es `unable to open database file`, la carpeta de datos no es del uid 1000 (típico si la escribió la 0.3.0, que corría como root): `sudo chown -R 1000:1000 ~/umbrel/app-data/ismaeloul-trajet/data` y reinicia la app. Si es otro, mira «Errores recientes» |
| Umbrel no instala: no puede descargar la imagen | el registro local está parado o no tiene esa versión: vuelve a lanzar `sudo bash scripts/publish.sh 0.4.0` |
| El panel (o `/api/*`) da 403 «solo acepta conexiones del proxy de Umbrel» | entras por un camino que no es el proxy. Con `TRAJET_ADMIN_PEERS` puedes abrirlo a una red (`192.168.1.0/24`) o a todo (`any`) |
| El iPhone no conecta | `curl http://<dirección del QR>/api/v1/ping` desde otro equipo tiene que responder sin login; si pide login, falta la lista blanca en la tienda |
| El panel avisa de que la clave no se puede descifrar | ha cambiado la semilla (`APP_SEED`) o falta `secrets/master.key`: vuelve a pegar la clave |

---

## Volver a la 0.3.0

La migración **solo añade** tablas y columnas: no borra ni cambia nada de lo
que ya había. Por eso la 0.3.0 sigue leyendo sin problemas una base de datos
migrada (comprobado), incluidas las rutas creadas desde el iPhone. Y antes de
migrar se hizo una copia exacta, `trajet.db.bak-v0`, al lado de `trajet.db`.

**Lo más sencillo**, si viniste del stack de `~/trajet` y aún no lo has
borrado: para Trajet en Umbrel y arranca el stack de siempre, que tiene su
propia BD sin tocar.

```bash
umbreld client apps.stop.mutate --appId ismaeloul-trajet
cd ~/trajet && sudo docker compose up -d
```

**Si quieres la base de datos exactamente como estaba antes de actualizar**
(lo hecho con la 0.4.0 se aparta, no se borra), siempre con la app parada:

```bash
umbreld client apps.stop.mutate --appId ismaeloul-trajet
cd ~/umbrel/app-data/ismaeloul-trajet/data
mkdir -p v040
# La BD migrada con sus -wal/-shm: van juntos. Un -wal que se queda al lado de
# la copia restaurada es de OTRA base de datos y la estropearía.
for f in trajet.db trajet.db-wal trajet.db-shm; do [ -e "$f" ] && mv "$f" v040/; done
cp -p trajet.db.bak-v0 trajet.db
```

Y luego usa esa BD con la imagen 0.3.0, que sigue en el registro local
(`localhost:5000/ismaeloul-trajet/trajet:0.3.0`): o bien copiando `trajet.db`
a `~/trajet/data/` y arrancando el stack viejo, o bien cambiando la imagen en
`~/umbrel/app-data/ismaeloul-trajet/docker-compose.yml` (`0.4.0` → `0.3.0`) y
arrancando la app. Ojo con esto último: la 0.3.0 no sabe nada del panel, así
que necesita la clave en `PRIM_API_KEY`, y la próxima actualización desde la
tienda volverá a poner la 0.4.0.

Si más adelante vuelves a la 0.4.0 con esa BD, se migra otra vez sola (y no
pisa la copia `bak-v0` que ya existe).

---

## Variables de entorno

Todas son opcionales. En el Umbrel las pone el `docker-compose.yml` de la
tienda; en este PC, el `.env` (ver `.env.example`).

| variable | por defecto | qué hace |
|---|---|---|
| `PRIM_API_KEY` | vacía | Clave de PRIM **de reserva**. La que se guarda en el panel manda sobre esta; esta solo se usa si no hay ninguna guardada (o si no se puede descifrar). |
| `TRAJET_SECRET_SEED` | vacía (en Umbrel, `${APP_SEED}`) | Semilla para cifrar la clave de PRIM en reposo. Sin ella se genera `secrets/master.key` y el panel avisa de que es la opción floja (la clave maestra viaja en la misma copia que la clave cifrada). |
| `APP_SEED` | la pone Umbrel | Se usa si no hay `TRAJET_SECRET_SEED`. |
| `TRAJET_DB` | `./data/trajet.db` (en la imagen, `/data/trajet.db`) | Ruta de la base de datos SQLite. |
| `TRAJET_DATA_DIR` | la carpeta de `TRAJET_DB` | Dónde van `secrets/` y las copias de las migraciones. |
| `TRAJET_ADMIN_PEERS` | `auto` | Quién puede hablar con el panel y con la API de la 0.3.0 (`/api/*`): `auto` (el proxy de Umbrel o `127.0.0.1`), `any`, o una lista de redes separadas por comas (`192.168.1.0/24,10.21.0.1`). |
| `APP_PROXY_HOSTNAME` | vacía | Nombre del contenedor del proxy de Umbrel, en las versiones que lo tienen aparte; lo que resuelva se suma a los pares de confianza de `auto`. En umbreld 2.0 no hace falta. |
| `TRAJET_LAN_URL` | vacía | Dirección de casa que va en el QR. Solo el valor inicial: lo guardado en el panel manda. |
| `TRAJET_TAILSCALE_URL` | vacía | Igual, la de Tailscale. |
| `TRAJET_SERVER_NAME` | `Trajet` | Nombre del servidor en el QR y en la app (40 caracteres como mucho). |
| `TRAJET_QUOTA_CAP` | `1000` | Tope propio de llamadas por endpoint y día UTC. `scripts/test-real.sh` lo baja a 800. |
| `TRAJET_COLLECT` | `1` | Recolector de andenes en segundo plano. `0`, `false`, `no` u `off` lo apagan (da igual mayúsculas). |
| `TRAJET_MAP` | `1` | Mapa con los datos abiertos de IDFM. `0` lo apaga. |
| `TRAJET_IDFM_PORTAL` | `https://data.iledefrance-mobilites.fr` | Portal de datos abiertos del mapa. |
| `OLLAMA_URL` | vacía (en Umbrel, `http://ollama_ollama_1:11434`) | Ollama para traducir los avisos. Vacía = sin traducción: los avisos salen en francés y lo demás funciona igual. |
| `OLLAMA_MODEL` | `gemma3:4b` | Modelo de Ollama. |
| `TRAJET_LOG_LEVEL` | `INFO` | Nivel de los logs. |
| `TZ` | `Europe/Paris` (la imagen) | Solo la hora de los logs: el código trabaja siempre con la hora de París y con UTC para la cuota. |

`WEB_CONCURRENCY` y `FORWARDED_ALLOW_IPS`, que uvicorn leería del entorno, no
tienen efecto: la imagen fija `--workers 1` y `--no-proxy-headers`.
