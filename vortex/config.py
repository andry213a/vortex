from __future__ import annotations

import json
import re
import secrets
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "Vortex"
APP_AUTHOR = "Vortex"
CONFIG_VERSION = 1

AUTH_MODES = ("auto", "manual", "none")

DEFAULT_PORT = 8080
DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024
DIRECT_UPLOAD_LIMIT = 100 * 1024 * 1024
MAX_AUDIT_BYTES = 10 * 1024 * 1024
AUDIT_BACKUPS = 5
LOGIN_MAX_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 15 * 60
TOKEN_TTL_SECONDS = 12 * 60 * 60


def app_config_dir() -> Path:
    path = Path(user_config_dir(APP_NAME, APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def app_data_dir() -> Path:
    path = Path(user_data_dir(APP_NAME, APP_AUTHOR))
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_auto_token() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    left = "".join(secrets.choice(alphabet) for _ in range(4))
    right = "".join(secrets.choice(string.digits + alphabet) for _ in range(4))
    return f"{left}-{right}"


@dataclass
class ServerConfig:
    base_dir: str = ""
    port: int = DEFAULT_PORT
    auth_mode: str = "auto"
    auto_token: str = field(default_factory=generate_auto_token)
    password_hash: str = ""
    guest_write: bool = True
    allow_guest: bool = True
    hostname: str = "vortex.local"
    chunk_size: int = DEFAULT_CHUNK_SIZE
    direct_upload_limit: int = DIRECT_UPLOAD_LIMIT
    manual_password: str = ""
    accent_color: str = "#60A5FA"

    def __post_init__(self) -> None:
        self.allow_guest = True
        self.guest_write = True

    def validate(self) -> None:
        self.allow_guest = True
        self.guest_write = True
        if self.auth_mode not in AUTH_MODES:
            raise ValueError(f"Modo de autenticación inválido: {self.auth_mode}")
        if not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("El puerto debe ser un entero entre 1 y 65535")
        if not self.base_dir:
            raise ValueError("Debe seleccionar una carpeta raíz para el servidor")
        base = Path(self.base_dir).expanduser()
        if not base.exists() or not base.is_dir():
            raise ValueError("La carpeta raíz seleccionada no existe o no es un directorio")
        if not self.auto_token:
            self.auto_token = generate_auto_token()
        if not isinstance(self.accent_color, str) or not re.fullmatch(
            r"#[0-9A-Fa-f]{6}", self.accent_color
        ):
            self.accent_color = "#60A5FA"
        if self.chunk_size < 256 * 1024:
            self.chunk_size = DEFAULT_CHUNK_SIZE
        if self.direct_upload_limit < self.chunk_size:
            self.direct_upload_limit = DIRECT_UPLOAD_LIMIT

    def resolved_base_dir(self) -> Path:
        return Path(self.base_dir).expanduser().resolve()

    def to_dict(self, persist: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if persist:
            data.pop("manual_password", None)
        data["config_version"] = CONFIG_VERSION
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServerConfig":
        known = {f for f in cls.__dataclass_fields__}
        values = {k: v for k, v in data.items() if k in known}
        values.pop("manual_password", None)
        return cls(**values)


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_config_dir() / "config.json")

    def load(self) -> ServerConfig:
        if not self.path.exists():
            return ServerConfig()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ServerConfig()
        if not isinstance(raw, dict):
            return ServerConfig()
        return ServerConfig.from_dict(raw)

    def save(self, config: ServerConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(config.to_dict(persist=True), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)
