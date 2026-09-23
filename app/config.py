"""Configuracion. Todo por variables de entorno, nada hardcodeado."""
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    api_key: str = os.environ.get("PRIM_API_KEY", "").strip()
    db_path: str = os.environ.get("TRAJET_DB", "./data/trajet.db")

    # Cuota: 1000 llamadas/dia POR ENDPOINT, reset a medianoche UTC.
    # Los TTL estan calculados para que un uso normal (mirar el movil unos
    # minutos por trayecto) no se acerque al limite. Ver README.
    ttl_stop_monitoring: int = 25    # el front refresca cada 30 s
    ttl_general_message: int = 150   # una sola llamada cubre TODAS las lineas
    ttl_journeys: int = 600          # PASO 2: caro, se cachea 10 min
    ttl_places: int = 86400          # el buscador de paradas no cambia
    ttl_line_info: int = 86400       # nombre y color de linea, tampoco

    refresh_seconds: int = 30
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Paris"))

    # Aprender el anden en segundo plano. Se puede apagar con
    # TRAJET_COLLECT=0 si algun dia la cuota va justa.
    collect: bool = os.environ.get("TRAJET_COLLECT", "1").strip() not in ("0", "false", "no")

    # LLM local para traducir los avisos del frances. Vacio = sin traduccion,
    # y la pantalla sigue funcionando igual con el texto original.
    ollama_url: str = os.environ.get("OLLAMA_URL", "").strip().rstrip("/")
    ollama_model: str = os.environ.get("OLLAMA_MODEL", "gemma3:4b").strip()


settings = Settings()
