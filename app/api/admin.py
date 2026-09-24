"""API del panel (/api/admin/*), detras del login de Umbrel.

Contrato: las operaciones `admin_*` de docs/openapi.yaml. Quien entra aqui es
solo el dueno, desde el navegador:

  - Todo el router pasa por auth.require_panel: la conexion tiene que venir
    del proxy de Umbrel (TRAJET_ADMIN_PEERS) y lo que modifica lleva
    `X-Trajet-Panel: 1` y el mismo origen (anti-CSRF). Cache-Control: no-store
    lo pone el middleware de api/etag.py.
  - Aqui solo hay HTTP: leer y validar, llamar a auth/prim/db y dar la forma
    del contrato. SQLite y todo lo sincrono va con run_in_threadpool.
  - La clave PRIM (R83): entra por POST /prim-key y no sale NUNCA; de ella solo
    se devuelve `last4`. Ningun mensaje de error repite lo que se mando.

Los cuerpos JSON se leen a mano (como en v1.py): asi un cuerpo malo da 400
con un motivo en espanol y sin eco del contenido, nunca un 422 de FastAPI.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .. import auth, db, logs, mapdata, prim
from .. import board as B
from .. import keystore as KS
from .. import platform as P
from .. import quota as Q
from .. import translate as T
from ..collector import collector
from ..config import VERSION, settings
from .errors import ApiError

log = logging.getLogger("trajet.admin")

router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_panel)])

# Lo mas grande que manda el panel son los ajustes (tres textos cortos).
_MAX_BODY = 16 * 1024

# Una clave de PRIM es texto ASCII visible, sin espacios. Si al pegarla se
# cuela un salto de linea en medio, httpx no podria ni mandar la cabecera y
# el error diria «no se puede conectar»: mejor decir lo que pasa.
_KEY_RE = re.compile(r"[\x21-\x7e]+")
KEY_MIN, KEY_MAX = 8, 256

MAX_SERVER_NAME = 40
MAX_URL = 200
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
    r"(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*$")
_ID_RE = re.compile(r"^[0-9]{1,12}$")

# Memoria del proceso y limite del contenedor (Linux). En Windows no existen
# y el panel dice «no disponible». Se cambian en los tests.
PROC_STATUS = "/proc/self/status"
CGROUP_LIMITS = ("/sys/fs/cgroup/memory.max",                      # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes")    # cgroup v1
# cgroup v1 dice «sin limite» con un numero enorme (2^63 redondeado a pagina).
_NO_LIMIT = 1 << 60

_LOADED_AT = time.time()


# =====================================================================
#  Utilidades
# =====================================================================

async def _read_object(request: Request) -> dict:
    """El cuerpo como objeto JSON, o ApiError bad_request (sin eco)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_BODY:
        raise ApiError("bad_request", "el cuerpo es demasiado grande")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > _MAX_BODY:
            raise ApiError("bad_request", "el cuerpo es demasiado grande")
    if not raw:
        raise ApiError("bad_request", "falta el cuerpo JSON")
    try:
        body = json.loads(bytes(raw))
    except (ValueError, UnicodeDecodeError) as e:
        raise ApiError("bad_request", "el cuerpo no es JSON válido") from e
    if not isinstance(body, dict):
        raise ApiError("bad_request", "el cuerpo tiene que ser un objeto JSON")
    return body


def _only(body: dict, allowed: set[str]) -> None:
    """additionalProperties: false del contrato. Solo se nombran los campos
    que sobran, nunca sus valores."""
    extra = sorted(k for k in body if k not in allowed)
    if extra:
        raise ApiError("bad_request", f"campos que no se esperan: {', '.join(extra)[:120]}")


def _clean_text(value: str) -> str:
    """Sin caracteres de control y con los espacios normalizados (lo mismo
    que hace auth con los nombres que se ensenan en el panel)."""
    value = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(value.split())


def _device_id(raw: str) -> int:
    # Un id que no es un numero es «no existe» (404), no un 400: el contrato
    # no declara 400 en DELETE y para quien llama es lo mismo.
    if not _ID_RE.match(raw or ""):
        raise ApiError("not_found", "no existe ese dispositivo")
    return int(raw)


def _pairing_id(raw: str) -> str:
    if not raw or len(raw) > 64:
        raise ApiError("not_found", "no existe ese emparejamiento")
    return raw


# =====================================================================
#  Resumen (overview)
# =====================================================================

def memory_usage() -> dict:
    """RSS del proceso (VmRSS) y limite del contenedor (cgroup). null si no
    se puede leer (Windows, macOS o un cgroup sin limite)."""
    rss = None
    try:
        with open(PROC_STATUS, encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024     # "VmRSS:  12345 kB"
                    break
    except (OSError, ValueError, IndexError):
        rss = None
    limit = None
    for path in CGROUP_LIMITS:
        try:
            with open(path, encoding="ascii") as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw == "max":                  # cgroup v2 sin limite
            break
        try:
            value = int(raw)
        except ValueError:
            continue
        if 0 < value < _NO_LIMIT:
            limit = value
        break
    return {"rss_bytes": rss, "limit_bytes": limit}


def _started_at() -> float:
    # app.main importa este modulo: se mira su STARTED_AT al llamar, no al
    # importar. Si el router se monta en otra app (tests), la hora de carga.
    try:
        from .. import main
        return float(main.STARTED_AT)
    except Exception:
        return _LOADED_AT


def _db_size() -> int:
    """Tamano de la BD en disco, con el WAL (puede ser de varios MB entre
    checkpoints y tambien es de la BD)."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(settings.db_path + suffix)
        except OSError:
            pass
    return total


def _safe(fn, default, what: str, failed: list[str]):
    """Una pieza del resumen que falla no tumba el panel: se pone su valor por
    defecto y se avisa arriba. El panel es justo donde hay que ver los fallos."""
    try:
        return fn()
    except Exception as e:
        log.warning("panel: no se pudo leer %s (%s)", what, type(e).__name__)
        failed.append(what)
        return default


def _overview_sync() -> dict:
    """Todo lo del resumen que lee SQLite o disco, en un solo viaje al hilo."""
    from ..migrations import LATEST

    failed: list[str] = []
    counts = _safe(db.counts, {}, "los contadores de la BD", failed)
    return {
        "schema_version": _safe(db.schema_version, 0, "la versión del esquema", failed),
        "schema_latest": LATEST,
        "prim_key": prim.get_keystore().info(),
        "quota": prim.quota_snapshot(),
        "devices": _safe(auth.active_device_count, 0, "los dispositivos", failed),
        "platform_model": _safe(P.accuracy, {"predictions": 0, "hits": 0, "rate": None,
                                             "observations": 0, "days": 0},
                                "la previsión de vía", failed),
        "database": {"size_bytes": _db_size(),
                     "routes": int(counts.get("routes", 0)),
                     "history": int(counts.get("history", 0)),
                     "platform_obs": int(counts.get("platform_obs", 0))},
        "memory": memory_usage(),
        "map_data": _safe(mapdata.status, {"cached_items": 0, "last_refresh": None,
                                           "last_error": "no se pudo leer el estado"},
                          "los datos del mapa", failed),
        "urls": _safe(auth.server_urls, [], "las direcciones del QR", failed),
        "failed": failed,
    }


def _spent_pct(ep: dict) -> int:
    """Porcentaje gastado de un endpoint con la cifra mas pesimista (la del
    contador o la que dice PRIM), igual que quota.py para el nivel."""
    cap = max(1, int(ep["cap"]))
    remaining = cap - int(ep["used"])
    if ep.get("remaining_reported") is not None:
        remaining = min(remaining, int(ep["remaining_reported"]))
    remaining = max(0, remaining)
    return round(100 * (cap - remaining) / cap)


def build_warnings(ov: dict, urls: list[dict], seed_configured: bool,
                   failed: list[str] | None = None) -> list[dict]:
    """Avisos de AdminOverview.warnings, de mas grave a menos.

    Cada uno dice que pasa y que hacer, en una frase. Nunca la clave."""
    errors: list[str] = []
    warns: list[str] = []
    infos: list[str] = []

    latest = ov.get("schema_latest")
    if latest and ov["schema_version"] < latest:
        errors.append(f"La base de datos no está al día (esquema v{ov['schema_version']} de "
                      f"v{latest}): la migración falló al arrancar; mira los errores recientes.")
    for what in failed or []:
        warns.append(f"No se pudo leer {what}: mira los errores recientes.")

    key = ov["prim_key"]
    state = key["state"]
    detail = (key.get("state_detail") or "").replace(KS.UNREADABLE_DETAIL, "").strip()
    if not key["configured"]:
        errors.append("No hay clave de PRIM: el iPhone no tendrá horarios hasta que guardes "
                      "una en «Clave de PRIM».")
    elif state == "invalid":
        errors.append(f"PRIM rechaza la clave en uso ({detail or 'no la reconoce'}): "
                      "pega una nueva en «Clave de PRIM».")
    elif state == "forbidden":
        errors.append(f"La clave de PRIM no tiene permiso para todas las APIs ({detail or 'HTTP 403'}): "
                      "suscríbela a las que falten en el portal de PRIM.")
    elif state == "unreachable":
        warns.append(f"PRIM no responde bien ({detail or 'sin respuesta'}): el iPhone ve la "
                     "última copia guardada.")
    elif state == "quota_exhausted" and ov["quota"]["level"] != "exhausted":
        warns.append(f"PRIM dice que la cuota de hoy está agotada ({detail or 'HTTP 429'}); "
                     "vuelve a medianoche UTC.")
    if KS.UNREADABLE_DETAIL in (key.get("state_detail") or ""):
        warns.append("Hay una clave guardada en el panel que no se puede descifrar (¿ha cambiado "
                     "APP_SEED?): vuelve a guardarla.")

    by_level: dict[str, list[str]] = {}
    for ep in ov["quota"]["endpoints"]:
        if ep["level"] != "ok":
            by_level.setdefault(ep["level"], []).append(
                f"{ep['endpoint']} al {_spent_pct(ep)} %")
    if by_level.get("exhausted"):
        errors.append(f"Cuota de PRIM agotada ({', '.join(by_level['exhausted'])}): se sirve la "
                      "última copia hasta la medianoche UTC.")
    if by_level.get("critical"):
        warns.append(f"Cuota de PRIM casi agotada ({', '.join(by_level['critical'])}): la app "
                     "refresca cada 2 minutos para que llegue a medianoche UTC.")
    if by_level.get("warn"):
        warns.append(f"Cuota de PRIM justa ({', '.join(by_level['warn'])}): la app refresca "
                     "más despacio.")

    if key.get("source") == "panel" and key.get("encryption") == "local_master_key":
        warns.append("La clave de PRIM está cifrada con una clave maestra local porque no hay "
                     "APP_SEED: una copia de /data basta para descifrarla. Pon APP_SEED en el "
                     "entorno y vuelve a guardarla.")
    elif not seed_configured and key.get("source") != "panel":
        infos.append("No hay APP_SEED: si guardas la clave en el panel se cifrará con una clave "
                      "maestra local, que es la opción menos segura.")

    if not urls:
        warns.append("El QR no lleva ninguna dirección del servidor: ponla en «Ajustes del QR» o "
                     "el iPhone no sabrá dónde conectarse.")

    tr = ov.get("translator") or {}
    if not tr.get("ok") and tr.get("reason") and tr.get("reason") != "sin configurar":
        infos.append(f"La traducción de avisos no funciona ({tr['reason'][:160]}): el iPhone "
                     "los enseña en francés.")

    map_error = (ov.get("map_data") or {}).get("last_error")
    if map_error:
        infos.append(f"Datos del mapa: {str(map_error)[:160]}. El mapa usa lo que ya tenía guardado.")

    return ([{"level": "error", "text": t} for t in errors]
            + [{"level": "warn", "text": t} for t in warns]
            + [{"level": "info", "text": t} for t in infos])


@router.get("/overview", operation_id="admin_overview")
async def admin_overview():
    """Todo lo del panel en una llamada (el panel la repite cada 15 s)."""
    data = await run_in_threadpool(_overview_sync)
    try:
        translator = await T.available_cached()
    except Exception as e:                  # Ollama nunca tumba el panel
        translator = {"ok": False, "reason": f"no se pudo comprobar ({type(e).__name__})"}
    ov = {
        "version": VERSION,
        "uptime_s": int(max(0.0, time.time() - _started_at())),
        "now_paris": B.now_paris().strftime("%Y-%m-%d %H:%M:%S"),
        "schema_version": int(data["schema_version"] or 0),
        "prim_key": data["prim_key"],
        "quota": data["quota"],
        "devices": int(data["devices"]),
        "collector": collector.status(),
        "platform_model": data["platform_model"],
        "translator": translator,
        "database": data["database"],
        "memory": data["memory"],
        "map_data": data["map_data"],
    }
    ov["warnings"] = build_warnings(dict(ov, schema_latest=data["schema_latest"]), data["urls"],
                                    bool(settings.secret_seed), data["failed"])
    return ov


# =====================================================================
#  Emparejamiento
# =====================================================================

@router.post("/pairing", operation_id="admin_pairing_create", status_code=201)
async def admin_pairing_create():
    """QR nuevo (5 min, un solo uso). Anula el anterior que siguiera vivo."""
    session = await run_in_threadpool(auth.new_pairing)
    return JSONResponse(session, status_code=201)


@router.get("/pairing/{pairing_id}", operation_id="admin_pairing_status")
async def admin_pairing_status(pairing_id: str):
    return await run_in_threadpool(auth.pairing_status, _pairing_id(pairing_id))


@router.delete("/pairing/{pairing_id}", operation_id="admin_pairing_cancel")
async def admin_pairing_cancel(pairing_id: str):
    return await run_in_threadpool(auth.cancel_pairing, _pairing_id(pairing_id))


# =====================================================================
#  Dispositivos
# =====================================================================

@router.get("/devices", operation_id="admin_devices")
async def admin_devices():
    return {"devices": await run_in_threadpool(auth.list_devices)}


@router.patch("/devices/{device_id}", operation_id="admin_device_rename")
async def admin_device_rename(device_id: str, request: Request):
    did = _device_id(device_id)
    body = await _read_object(request)
    _only(body, {"name"})
    if "name" not in body:
        raise ApiError("bad_request", "falta 'name'")
    return await run_in_threadpool(auth.rename_device, did, body["name"])


@router.delete("/devices/{device_id}", operation_id="admin_device_revoke")
async def admin_device_revoke(device_id: str):
    """El token deja de valer en la siguiente peticion (no hay cache de tokens)."""
    did = _device_id(device_id)
    if not await run_in_threadpool(auth.revoke_device, did):
        raise ApiError("not_found", "no existe ese dispositivo o ya estaba revocado")
    return {"revoked": did}


# =====================================================================
#  Clave PRIM (R83, R84)
# =====================================================================

@router.get("/prim-key", operation_id="admin_prim_key_info")
async def admin_prim_key_info():
    return await run_in_threadpool(lambda: prim.get_keystore().info())


@router.post("/prim-key", operation_id="admin_prim_key_save")
async def admin_prim_key_save(request: Request):
    """Probar y guardar (o reemplazar). 200 si se guardo y ya esta en uso;
    422 con el mismo cuerpo (PrimKeyResult) si PRIM la rechaza o no se pudo
    comprobar, y entonces no se guarda nada."""
    body = await _read_object(request)
    _only(body, {"key"})
    key = body.get("key")
    if not isinstance(key, str):
        raise ApiError("bad_request", "falta 'key' o no es texto")
    key = key.strip()
    # Antes de nada: cualquier log de aqui en adelante ya la tacha.
    logs.register_secret(key)
    if not KEY_MIN <= len(key) <= KEY_MAX:
        raise ApiError("bad_request", f"la clave tiene que tener de {KEY_MIN} a {KEY_MAX} caracteres")
    if not _KEY_RE.fullmatch(key):
        raise ApiError("bad_request", "la clave solo puede tener letras, cifras y signos, sin "
                       "espacios ni saltos de línea: vuelve a copiarla del portal de PRIM")
    result = await prim.set_key_and_save(key)
    return JSONResponse(result, status_code=200 if result.get("saved") else 422)


@router.delete("/prim-key", operation_id="admin_prim_key_delete")
async def admin_prim_key_delete(request: Request):
    """Borra la del panel. Si hay PRIM_API_KEY en el entorno, pasa a usarse
    esa (en caliente); si no, el servidor se queda sin clave."""
    if request.headers.get("x-trajet-confirm", "").strip() != "borrar":
        raise ApiError("bad_request", "falta la cabecera X-Trajet-Confirm: borrar (evita borrar "
                       "la clave sin querer)")
    return await prim.delete_saved_key()


@router.post("/prim-key/check", operation_id="admin_prim_key_check")
async def admin_prim_key_check():
    """Vuelve a probar la clave en uso (una llamada por API)."""
    return await prim.recheck()


# =====================================================================
#  Cuota y errores
# =====================================================================

def _quota() -> dict:
    counter = prim.client.quota_counter if prim.client is not None else Q.get_quota()
    return {"today": counter.snapshot(), "history": counter.history(7)}


@router.get("/quota", operation_id="admin_quota")
async def admin_quota():
    return await run_in_threadpool(_quota)


@router.get("/errors", operation_id="admin_errors")
async def admin_errors(limit: str | None = None):
    """Del mas nuevo al mas viejo. Un `limit` raro no es un error (el
    contrato no declara 400): se usa el de siempre o se ajusta a 1..200."""
    try:
        n = int(limit) if limit not in (None, "") else 50
    except ValueError:
        n = 50
    n = max(1, min(n, 200))
    return {"errors": await run_in_threadpool(logs.recent_errors, n)}


# =====================================================================
#  Ajustes: nombre y direcciones del QR
# =====================================================================

def normalize_base_url(value, label: str) -> str | None:
    """`http(s)://host[:puerto]` sin ruta, o None si viene vacia.

    El puerto es opcional: por Tailscale Serve la direccion suele ser
    https://nas.xxxx.ts.net, sin puerto. Lo que no se admite es ruta,
    parametros, usuario o contrasena: la app le anade /api/v1 detras."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("bad_request", f"{label}: tiene que ser texto o null")
    v = value.strip()
    if not v:
        return None
    if len(v) > MAX_URL:
        raise ApiError("bad_request", f"{label}: demasiado larga")
    v = v.rstrip("/")
    parts = urlsplit(v)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise ApiError("bad_request", f"{label}: tiene que ser http://host:puerto o https://host")
    if parts.path or parts.query or parts.fragment or "?" in v or "#" in v:
        raise ApiError("bad_request", f"{label}: solo host y puerto, sin ruta ni parámetros "
                       "(p. ej. http://192.168.1.10:7796)")
    if "@" in parts.netloc:
        raise ApiError("bad_request", f"{label}: sin usuario ni contraseña")
    try:
        port = parts.port
    except ValueError:
        raise ApiError("bad_request", f"{label}: el puerto no es válido") from None
    if port == 0:
        raise ApiError("bad_request", f"{label}: el puerto no es válido")
    host = (parts.hostname or "").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        if not _HOST_RE.match(host):
            raise ApiError("bad_request", f"{label}: el nombre del servidor no es válido") from None
    netloc = f"[{host}]" if ip is not None and ip.version == 6 else host
    if port is not None:
        netloc += f":{port}"
    return f"{scheme}://{netloc}"


def _settings() -> dict:
    """Lo que de verdad va en el QR (panel > entorno)."""
    urls = {u["kind"]: u["url"] for u in auth.server_urls()}
    return {"server_name": auth.server_name(), "lan_url": urls.get("lan"),
            "tailscale_url": urls.get("tailscale")}


def _save_settings(name: str, lan: str | None, ts: str | None) -> dict:
    """Guarda solo lo que se aparta del entorno.

    settings_kv: fila con valor = ese valor; fila vacia = «ninguna» aunque
    el entorno tenga una; sin fila = la del entorno. Si lo que llega es lo
    mismo que dice el entorno se borra la fila, para que un cambio futuro
    del entorno se vea sin tener que tocar el panel."""
    env_name = (settings.server_name or "Trajet").strip()
    db.set_setting("server_name", None if not name or name == env_name else name)
    for key, value, env in (("lan_url", lan, settings.lan_url),
                            ("tailscale_url", ts, settings.tailscale_url)):
        env = (env or "").strip().rstrip("/")
        if (value or "") == env:
            db.set_setting(key, None)
        else:
            db.set_setting(key, value or "")
    return _settings()


@router.get("/settings", operation_id="admin_settings_get")
async def admin_settings_get():
    return await run_in_threadpool(_settings)


@router.put("/settings", operation_id="admin_settings_put")
async def admin_settings_put(request: Request):
    body = await _read_object(request)
    _only(body, {"server_name", "lan_url", "tailscale_url"})
    missing = [k for k in ("server_name", "lan_url", "tailscale_url") if k not in body]
    if missing:
        raise ApiError("bad_request", f"faltan campos: {', '.join(missing)}")
    name = body["server_name"]
    if not isinstance(name, str):
        raise ApiError("bad_request", "server_name: tiene que ser texto")
    name = _clean_text(name)
    if len(name) > MAX_SERVER_NAME:
        raise ApiError("bad_request", f"server_name: como mucho {MAX_SERVER_NAME} caracteres")
    lan = normalize_base_url(body["lan_url"], "lan_url")
    ts = normalize_base_url(body["tailscale_url"], "tailscale_url")
    out = await run_in_threadpool(_save_settings, name, lan, ts)
    log.info("panel: ajustes guardados (direcciones del QR: %d)",
             int(bool(out["lan_url"])) + int(bool(out["tailscale_url"])))
    return out
