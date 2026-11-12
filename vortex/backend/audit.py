from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

from ..config import AUDIT_BACKUPS, MAX_AUDIT_BYTES

INFO = "INFO"
WARNING = "WARNING"
ERROR = "ERROR"
SECURITY = "SECURITY"

ACCIONES = {
    "SERVER_START",
    "SERVER_STOP",
    "PRIVILEGE_REQUEST",
    "LOGIN_SUCCESS",
    "LOGIN_FAILED",
    "RATE_LIMIT_BLOCK",
    "PATH_TRAVERSAL",
    "FILE_UPLOAD",
    "FILE_DOWNLOAD",
    "FILE_DELETE",
    "FILE_RENAME",
    "FILE_MKDIR",
    "FILE_WRITE",
    "FILE_ZIP",
    "UNAUTHORIZED",
}


class _LineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def log_standalone(**kwargs: Any) -> dict[str, Any]:
    """Registra un evento sin depender de un Runtime (p. ej. antes de arrancar el servidor)."""
    from ..config import app_data_dir

    logger = AuditLogger(app_data_dir() / "logs" / "audit.log")
    return logger.log(**kwargs)


class AuditLogger:
    def __init__(self, log_file: Path) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = log_file
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._logger = logging.getLogger(f"vortex.audit.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = RotatingFileHandler(
                log_file,
                maxBytes=MAX_AUDIT_BYTES,
                backupCount=AUDIT_BACKUPS,
                encoding="utf-8",
            )
            handler.setFormatter(_LineFormatter())
            self._logger.addHandler(handler)

    def log(
        self,
        *,
        action: str,
        severity: str = INFO,
        event_id: str | None = None,
        client_ip: str = "",
        user_agent: str = "",
        target_path: str = "",
        status_code: int = 0,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event_id": event_id or uuid.uuid4().hex[:12],
            "severity": severity,
            "client_ip": client_ip or "",
            "user_agent": user_agent or "",
            "action": action,
            "target_path": target_path or "",
            "status_code": int(status_code or 0),
            "details": details or {},
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._logger.info(line)
        self._broadcast(record)
        return record

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def remove() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return remove

    def _broadcast(self, record: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(record)
            except Exception:
                continue

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        try:
            lines = self.log_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        result: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
