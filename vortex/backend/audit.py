# Registro de auditoría JSON rotativo y difusión a listeners en vivo.

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

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


# Registro de eventos antes de que exista un Runtime activo.
def log_standalone(**kwargs: Any) -> dict[str, Any]:
    """Registra un evento sin depender de un Runtime (p. ej. antes de arrancar el servidor)."""
    from ..config import app_data_dir

    registrador = AuditLogger(app_data_dir() / "logs" / "audit.log")
    return registrador.log(**kwargs)


# Logger JSON con rotación, listeners y cola de auditoría.
class AuditLogger:
    def __init__(self, log_file: Path) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = log_file
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._logger = logging.getLogger(f"vortex.audit.{uuid.uuid4().hex}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            manejador = RotatingFileHandler(
                log_file,
                maxBytes=MAX_AUDIT_BYTES,
                backupCount=AUDIT_BACKUPS,
                encoding="utf-8",
            )
            manejador.setFormatter(_LineFormatter())
            self._logger.addHandler(manejador)

    # Construye el evento, lo escribe en disco y lo difunde en vivo.
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
        registro = {
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
        linea = json.dumps(registro, ensure_ascii=False, default=str)
        self._logger.info(linea)
        self._broadcast(registro)
        return registro

    # Suscribe un callback y devuelve la función para cancelarlo.
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
            escuchadores = list(self._listeners)
        for retorno in escuchadores:
            try:
                retorno(record)
            except Exception:
                continue

    # Devuelve los últimos eventos válidos ignorando líneas corruptas.
    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.log_file.exists():
            return []
        try:
            lineas = self.log_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        resultado: list[dict[str, Any]] = []
        for linea in lineas[-limit:]:
            try:
                resultado.append(json.loads(linea))
            except json.JSONDecodeError:
                continue
        return resultado
