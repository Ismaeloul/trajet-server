"""Clave PRIM guardada desde el panel, cifrada en reposo.

Por que cifrarla si el fichero ya es 0600: Umbrel hace copias de /data (y se
pueden llevar a otro disco o a otra maquina). Con AES-256-GCM y la clave de
cifrado derivada de APP_SEED, el fichero suelto no sirve de nada: hace falta
ademas la semilla, que vive fuera de /data. Sin semilla se genera una clave
maestra local (secrets/master.key); es la opcion menos buena, porque viaja en
la misma copia que el fichero cifrado, y el panel lo avisa.

Reglas que se cumplen aqui (R83, R84):
  - La clave en claro solo vive en la memoria del proceso. Nunca se escribe
    en disco, ni en logs, ni en excepciones: de ella solo sale `last4`.
  - La del panel manda sobre PRIM_API_KEY del entorno; la del entorno queda
    como alternativa si se borra la del panel (o si no se puede descifrar).
  - Escritura atomica (temporal + os.replace): un corte de luz a mitad de un
    guardado deja la clave vieja o la nueva, nunca un fichero a medias.
  - Si el fichero no se puede descifrar (ha cambiado APP_SEED, falta
    master.key...) NO se borra ni se tumba nada: se sigue con la del entorno
    y se avisa en `state_detail` y en el log para que se vuelva a guardar.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import settings

log = logging.getLogger("trajet.keystore")

FILE_NAME = "prim-key.json"
MASTER_NAME = "master.key"
FORMAT_VERSION = 1

# El `info` de HKDF separa usos: si algun dia la misma semilla cifra otra
# cosa, sale otra clave. La AAD ata el texto cifrado a este formato: un
# fichero de otro sitio pegado aqui no se descifra.
HKDF_INFO = b"trajet/prim-key/v1"
AAD = b"trajet:prim-key:v1"
SALT_BYTES = 16
NONCE_BYTES = 12

STATES = ("missing", "valid", "invalid", "forbidden", "quota_exhausted",
          "unreachable", "unknown")

UNREADABLE_DETAIL = (
    "Hay una clave guardada en el panel que no se puede descifrar "
    "(¿ha cambiado APP_SEED o falta secrets/master.key?). Vuelve a guardarla.")
MISSING_DETAIL = "No hay clave de PRIM: guárdala en el panel."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(str(text).encode("ascii"), validate=True)


def _last4(key: str) -> str | None:
    return key[-4:] if len(key) >= 4 else None


class KeyStore:
    """La clave PRIM en uso y de donde sale (panel > entorno > ninguna)."""

    def __init__(self, data_dir: str, seed: str | None, env_key: str):
        self.dir = os.path.join(data_dir, "secrets")
        self.path = os.path.join(self.dir, FILE_NAME)
        self.master_path = os.path.join(self.dir, MASTER_NAME)
        self._seed = (seed or "").strip()
        self._env_key = (env_key or "").strip()
        # _lock protege lo que hay en memoria y solo se tiene un instante: lo
        # toma el bucle de eventos en cada tablero. _io ordena las escrituras
        # en disco (van en hilos) sin bloquear a quien solo lee.
        self._lock = threading.Lock()
        self._io = threading.Lock()
        # Clave del panel ya descifrada (solo memoria) y el documento del
        # fichero tal cual (cifrado), para reescribir el estado sin tener
        # que volver a cifrar.
        self._panel_key: str | None = None
        self._doc: dict = {}
        self._unreadable = False
        # La clave del entorno no tiene fichero: su estado vive en memoria y
        # se vuelve a comprobar tras cada arranque.
        self._env_state = {"state": "unknown", "state_detail": "", "checked_at": None}
        self._load()

    # ---------------- leer ----------------

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            key = self._decrypt(doc)
        except FileNotFoundError:
            return
        except (InvalidTag, ValueError, KeyError, TypeError, AttributeError, OSError) as e:
            # Solo el tipo de error: el mensaje de cryptography no lleva la
            # clave, pero asi no hay ni que pensarlo.
            self._unreadable = True
            log.warning("la clave PRIM guardada en el panel no se puede descifrar (%s); "
                        "se usa la del entorno si la hay", type(e).__name__)
            return
        self._panel_key = key
        self._doc = doc

    def _derive(self, enc: str, salt: bytes, create_master: bool = False) -> bytes:
        if enc == "app_seed":
            if not self._seed:
                raise ValueError("sin semilla")
            ikm = self._seed.encode("utf-8")
        elif enc == "local_master_key":
            ikm = self._master(create=create_master)
        else:
            raise ValueError("cifrado desconocido")
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                    info=HKDF_INFO).derive(ikm)

    def _decrypt(self, doc: dict) -> str:
        if int(doc.get("v", 0)) != FORMAT_VERSION or doc.get("kdf") != "HKDF-SHA256":
            raise ValueError("formato desconocido")
        salt = _b64d(doc["salt"])
        nonce = _b64d(doc["nonce"])
        if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES:
            raise ValueError("salt o nonce con tamano raro")
        k = self._derive(doc.get("enc", ""), salt)
        key = AESGCM(k).decrypt(nonce, _b64d(doc["ct"]), AAD).decode("utf-8")
        if not key:
            raise ValueError("clave vacia")
        return key

    def _master(self, create: bool) -> bytes:
        """Clave maestra local, solo cuando no hay APP_SEED."""
        try:
            with open(self.master_path, "rb") as f:
                raw = f.read()
            if len(raw) == 32:
                return raw
            if not create:
                raise ValueError("master.key no valida")
        except FileNotFoundError:
            if not create:
                raise ValueError("falta master.key") from None
        # Solo se (re)genera al GUARDAR: al leer, una maestra rota significa
        # que la clave vieja ya no se puede recuperar, y eso se avisa.
        self._ensure_dir()
        raw = secrets.token_bytes(32)
        self._write_atomic(self.master_path, raw)
        return raw

    # ---------------- escribir ----------------

    def _ensure_dir(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)      # makedirs respeta la umask
        except OSError:
            pass

    def _write_atomic(self, path: str, data: bytes) -> None:
        """Temporal 0600 en la misma carpeta + os.replace (atomico)."""
        tmp = f"{path}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        # En POSIX el rename solo es duradero tras sincronizar la carpeta.
        if os.name == "posix":
            try:
                dfd = os.open(os.path.dirname(path), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass

    def _write_doc(self, doc: dict) -> None:
        self._ensure_dir()
        self._write_atomic(self.path, json.dumps(doc, indent=1).encode("utf-8"))

    # ---------------- interfaz ----------------

    def current(self) -> tuple[str, str]:
        """(clave, origen): "panel", "env" o "none" (clave vacia)."""
        with self._lock:
            if self._panel_key:
                return self._panel_key, "panel"
            if self._env_key:
                return self._env_key, "env"
            return "", "none"

    def state(self) -> tuple[str, str, str | None]:
        """(estado, detalle, checked_at) de la clave en uso. Solo memoria:
        se llama en cada tablero y no puede tocar el disco."""
        with self._lock:
            if self._panel_key:
                st = self._doc.get("state") or "unknown"
                detail = self._doc.get("state_detail") or ""
                checked = self._doc.get("checked_at")
            elif self._env_key:
                st = self._env_state["state"]
                detail = self._env_state["state_detail"]
                checked = self._env_state["checked_at"]
            else:
                st, detail, checked = "missing", MISSING_DETAIL, None
            if self._unreadable:
                detail = f"{UNREADABLE_DETAIL} {detail}".strip()
            return st, detail, checked

    def info(self) -> dict:
        """PrimKeyInfo de docs/openapi.yaml. Nunca la clave: solo last4."""
        key, source = self.current()
        st, detail, checked = self.state()
        with self._lock:
            panel = source == "panel"
            return {
                "configured": bool(key),
                "source": source,
                "last4": _last4(key) if key else None,
                "saved_at": self._doc.get("saved_at") if panel else None,
                "state": st,
                "state_detail": detail,
                "checked_at": checked,
                "env_available": bool(self._env_key),
                "encryption": (self._doc.get("enc") or "none") if panel else "none",
            }

    def save(self, key: str, state: str = "unknown", detail: str = "") -> None:
        """Cifra y guarda la clave (reemplaza la que hubiera)."""
        key = (key or "").strip()
        if not key:
            raise ValueError("la clave esta vacia")
        if state not in STATES:
            raise ValueError(f"estado desconocido: {state}")
        with self._io:
            enc = "app_seed" if self._seed else "local_master_key"
            salt = os.urandom(SALT_BYTES)
            nonce = os.urandom(NONCE_BYTES)
            k = self._derive(enc, salt, create_master=True)
            ct = AESGCM(k).encrypt(nonce, key.encode("utf-8"), AAD)
            now = _now_iso()
            doc = {
                "v": FORMAT_VERSION, "alg": "AES-256-GCM", "kdf": "HKDF-SHA256",
                "enc": enc, "salt": _b64e(salt), "nonce": _b64e(nonce),
                "ct": _b64e(ct), "last4": _last4(key), "saved_at": now,
                "state": state, "state_detail": detail or "", "checked_at": now,
            }
            self._write_doc(doc)
            with self._lock:
                self._panel_key = key
                self._doc = doc
                self._unreadable = False
        if enc == "local_master_key":
            log.warning("clave PRIM cifrada con una clave maestra local (sin APP_SEED)")

    def delete(self) -> None:
        """Borra la clave del panel. Si hay PRIM_API_KEY, pasa a usarse esa."""
        with self._io:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            with self._lock:
                self._panel_key = None
                self._doc = {}
                self._unreadable = False

    def set_state(self, state: str, detail: str = "") -> None:
        """Estado de la clave EN USO tras comprobarla o tras una respuesta
        de PRIM. La del panel lo guarda en su fichero; la del entorno, en
        memoria."""
        if state not in STATES:
            raise ValueError(f"estado desconocido: {state}")
        now = _now_iso()
        with self._io:
            with self._lock:
                if self._panel_key:
                    doc = dict(self._doc, state=state, state_detail=detail or "",
                               checked_at=now)
                    self._doc = doc
                else:
                    doc = None
                    if self._env_key:
                        self._env_state = {"state": state, "state_detail": detail or "",
                                           "checked_at": now}
            if doc is not None:
                try:
                    self._write_doc(doc)
                except OSError as e:
                    # El estado es informativo: si el disco falla, se queda
                    # en memoria y la clave sigue funcionando.
                    log.warning("no se pudo guardar el estado de la clave PRIM: %s",
                                type(e).__name__)


# ---------------- accesor de modulo ----------------

_store: KeyStore | None = None
_store_sig: tuple | None = None


def get_store() -> KeyStore:
    """El almacen de la configuracion actual.

    Se rehace si cambian la carpeta de datos, la semilla o la clave del
    entorno (los tests cambian el entorno entre prueba y prueba y llaman a
    settings.reload()).
    """
    global _store, _store_sig
    sig = (os.path.abspath(settings.data_dir), settings.secret_seed, settings.api_key)
    if _store is None or _store_sig != sig:
        _store = KeyStore(settings.data_dir, settings.secret_seed, settings.api_key)
        _store_sig = sig
    return _store


def reset_store() -> None:
    """Olvida el almacen en memoria; el siguiente get_store() relee el disco."""
    global _store, _store_sig
    _store = None
    _store_sig = None
