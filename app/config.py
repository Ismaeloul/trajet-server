"""Configuracion. Todo por variables de entorno, nada hardcodeado.

`settings` es un objeto unico que todos los modulos importan con
`from .config import settings`. Por eso NO se sustituye nunca: los tests
cambian el entorno y llaman a `settings.reload()`, que vuelve a leerlo sobre
el mismo objeto.
"""
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

VERSION = "0.4.0"
API_VERSION = 1


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: str = "1") -> bool:
    # "False", "no", "0" y "off" apagan, sin importar mayusculas (antes
    # TRAJET_COLLECT=False dejaba el recolector encendido).
    return _env(name, default).lower() not in ("0", "false", "no", "off", "")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # Clave PRIM del entorno. Desde la v2 es solo el valor inicial o la
    # alternativa: manda la que se guarde en el panel (ver keystore.py).
    api_key: str = ""
    db_path: str = "./data/trajet.db"
    # Carpeta de datos: la BD, los secretos (/data/secrets) y las copias
    # de seguridad de las migraciones.
    data_dir: str = "./data"

    # Semilla para cifrar la clave PRIM en reposo. En Umbrel es ${APP_SEED}.
    secret_seed: str = ""

    # Cuota: 1000 llamadas/dia POR ENDPOINT, reset a medianoche UTC.
    # Los TTL estan calculados para que un uso normal (mirar el movil unos
    # minutos por trayecto) no se acerque al limite. Ver docs/servidor.md.
    quota_cap: int = 1000
    ttl_stop_monitoring: int = 25    # el front refresca cada 30 s
    ttl_general_message: int = 150   # una sola llamada cubre TODAS las lineas
    ttl_journeys: int = 600          # PASO 2: caro, se cachea 10 min
    ttl_places: int = 86400          # el buscador de paradas no cambia
    ttl_line_info: int = 86400       # nombre y color de linea, tampoco

    refresh_seconds: int = 30
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Paris"))

    # Aprender el anden en segundo plano. Se puede apagar con
    # TRAJET_COLLECT=0 si algun dia la cuota va justa.
    collect: bool = True

    # LLM local para traducir los avisos del frances. Vacio = sin traduccion,
    # y la pantalla sigue funcionando igual con el texto original.
    ollama_url: str = ""
    ollama_model: str = "gemma3:4b"

    # Emparejamiento: direcciones que van en el QR (se pueden cambiar en el
    # panel; esto es solo el valor inicial) y nombre del servidor.
    lan_url: str = ""
    tailscale_url: str = ""
    server_name: str = "Trajet"

    # Quien puede hablar con el panel: "auto" (solo el proxy de Umbrel o
    # 127.0.0.1), "any" o una lista de redes separadas por comas.
    admin_peers: str = "auto"

    # Mapa: datos abiertos de IDFM. 0 lo apaga (el resto funciona igual).
    map_enabled: bool = True
    idfm_portal: str = "https://data.iledefrance-mobilites.fr"

    log_level: str = "INFO"

    def reload(self) -> "Settings":
        """Vuelve a leer el entorno sobre ESTE objeto."""
        self.api_key = _env("PRIM_API_KEY")
        self.db_path = _env("TRAJET_DB", "./data/trajet.db")
        self.data_dir = _env("TRAJET_DATA_DIR") or (
            os.path.dirname(os.path.abspath(self.db_path)) or "./data")
        self.secret_seed = _env("TRAJET_SECRET_SEED") or _env("APP_SEED")
        self.quota_cap = max(1, _int("TRAJET_QUOTA_CAP", 1000))
        self.collect = _flag("TRAJET_COLLECT", "1")
        self.ollama_url = _env("OLLAMA_URL").rstrip("/")
        self.ollama_model = _env("OLLAMA_MODEL", "gemma3:4b")
        self.lan_url = _env("TRAJET_LAN_URL").rstrip("/")
        self.tailscale_url = _env("TRAJET_TAILSCALE_URL").rstrip("/")
        self.server_name = _env("TRAJET_SERVER_NAME", "Trajet")[:40] or "Trajet"
        self.admin_peers = _env("TRAJET_ADMIN_PEERS", "auto") or "auto"
        self.map_enabled = _flag("TRAJET_MAP", "1")
        self.idfm_portal = _env(
            "TRAJET_IDFM_PORTAL", "https://data.iledefrance-mobilites.fr").rstrip("/")
        self.log_level = _env("TRAJET_LOG_LEVEL", "INFO").upper() or "INFO"
        return self


settings = Settings().reload()
