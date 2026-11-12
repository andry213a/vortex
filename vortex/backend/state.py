from __future__ import annotations

import time
from pathlib import Path

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
        self.rate_limiter = RateLimiter()
        if config.auth_mode == "manual" and config.manual_password:
            config.password_hash = AuthManager.hash_password(config.manual_password)
        if config.auth_mode == "manual" and not config.password_hash:
            raise ValueError("El modo de autenticación manual requiere una contraseña")
        self.auth = AuthManager(config)
        self.uploads = UploadManager(self.data_dir / "uploads", self.base_dir, config.chunk_size)
        self.uploads.cleanup_stale()
        self.mdns = MDNSAdvertiser(config.hostname, config.port)

    def resolve(self, relative: str) -> Path:
        return resolve_within(self.base_dir, relative or "")

    def relative(self, target: Path) -> str:
        return relative_to(self.base_dir, target)

    def unique(self, directory: Path, filename: str) -> Path:
        return unique_path(directory, filename)

    def uptime(self) -> int:
        return int(time.time() - self.started_at)
