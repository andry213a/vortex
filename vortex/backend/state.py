# Estado compartido del servidor en ejecución y cambios de archivos.

from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path, PurePosixPath

from ..config import ServerConfig, app_data_dir
from .audit import AuditLogger
from .auth import AuthManager
from .mdns import MDNSAdvertiser
from .security import RateLimiter, relative_to, resolve_within
from .transfer import UploadManager, unique_path


# Estado compartido del servidor y sincronización de cambios en disco.
class Runtime:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.base_dir = config.resolved_base_dir()
        self.started_at = time.time()
        self.data_dir = app_data_dir()
        self.audit = AuditLogger(self.data_dir / "logs" / "audit.log")
        self._file_change_lock = threading.Lock()
        self._file_change_revision = 0
        self._file_changes: deque[dict] = deque(maxlen=256)
        self.audit.add_listener(self._record_file_change)
        self.rate_limiter = RateLimiter()
        if config.auth_mode == "manual" and config.manual_password:
            config.password_hash = AuthManager.hash_password(config.manual_password)
        if config.auth_mode == "manual" and not config.password_hash:
            raise ValueError("El modo de autenticación manual requiere una contraseña")
        self.auth = AuthManager(config)
        self.uploads = UploadManager(self.data_dir / "uploads", self.base_dir, config.chunk_size)
        self.uploads.cleanup_stale()
        self.mdns = MDNSAdvertiser(config.hostname, config.port)

    # Clasifica un evento del sistema de archivos y actualiza la revisión.
    def _record_file_change(self, record: dict) -> None:
        accion = record.get("action")
        if accion not in {
            "FILE_UPLOAD", "FILE_WRITE", "FILE_DELETE", "FILE_RENAME",
            "FILE_MKDIR", "FILE_VERSION_RESTORE",
        }:
            return
        objetivo = str(record.get("target_path") or "")
        detalles = record.get("details") or {}
        rutas = {self._parent_path(objetivo)}
        cambio: dict = {"paths": sorted(rutas), "action": accion}
        if accion == "FILE_RENAME":
            origen = str(detalles.get("from") or "")
            rutas.add(self._parent_path(origen))
            cambio["paths"] = sorted(rutas)
            if detalles.get("is_dir"):
                cambio["renamed"] = {"from": origen, "to": objetivo}
        elif accion == "FILE_DELETE" and detalles.get("is_dir"):
            cambio["removed"] = objetivo
        with self._file_change_lock:
            self._file_change_revision += 1
            cambio["revision"] = self._file_change_revision
            self._file_changes.append(cambio)

    @staticmethod
    def _parent_path(path: str) -> str:
        padre = PurePosixPath(path).parent.as_posix()
        return "" if padre == "." else padre

    # Devuelve los cambios posteriores a una revisión para el sondeo web.
    def file_changes_since(self, revision: int) -> dict:
        with self._file_change_lock:
            return {
                "revision": self._file_change_revision,
                "changes": [elemento.copy() for elemento in self._file_changes if elemento["revision"] > revision],
            }

    def resolve(self, relative: str) -> Path:
        return resolve_within(self.base_dir, relative or "")

    def relative(self, target: Path) -> str:
        return relative_to(self.base_dir, target)

    def unique(self, directory: Path, filename: str) -> Path:
        return unique_path(directory, filename)

    def uptime(self) -> int:
        return int(time.time() - self.started_at)
