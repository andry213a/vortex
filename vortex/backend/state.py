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

    def _record_file_change(self, record: dict) -> None:
        action = record.get("action")
        if action not in {
            "FILE_UPLOAD", "FILE_WRITE", "FILE_DELETE", "FILE_RENAME",
            "FILE_MKDIR", "FILE_VERSION_RESTORE",
        }:
            return
        target = str(record.get("target_path") or "")
        details = record.get("details") or {}
        paths = {self._parent_path(target)}
        change: dict = {"paths": sorted(paths), "action": action}
        if action == "FILE_RENAME":
            source = str(details.get("from") or "")
            paths.add(self._parent_path(source))
            change["paths"] = sorted(paths)
            if details.get("is_dir"):
                change["renamed"] = {"from": source, "to": target}
        elif action == "FILE_DELETE" and details.get("is_dir"):
            change["removed"] = target
        with self._file_change_lock:
            self._file_change_revision += 1
            change["revision"] = self._file_change_revision
            self._file_changes.append(change)

    @staticmethod
    def _parent_path(path: str) -> str:
        parent = PurePosixPath(path).parent.as_posix()
        return "" if parent == "." else parent

    def file_changes_since(self, revision: int) -> dict:
        with self._file_change_lock:
            return {
                "revision": self._file_change_revision,
                "changes": [item.copy() for item in self._file_changes if item["revision"] > revision],
            }

    def resolve(self, relative: str) -> Path:
        return resolve_within(self.base_dir, relative or "")

    def relative(self, target: Path) -> str:
        return relative_to(self.base_dir, target)

    def unique(self, directory: Path, filename: str) -> Path:
        return unique_path(directory, filename)

    def uptime(self) -> int:
        return int(time.time() - self.started_at)
