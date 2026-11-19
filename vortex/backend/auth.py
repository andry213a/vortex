# Emisión y verificación de JWT efímeros y hash de contraseñas.

from __future__ import annotations

import hmac
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.hash import pbkdf2_sha256

from ..config import TOKEN_TTL_SECONDS, ServerConfig


# Emisión y verificación de tokens JWT y credenciales del host.
class AuthManager:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._secret = secrets.token_urlsafe(48)
        self._algorithm = "HS256"
        self._active: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def hash_password(raw: str) -> str:
        return pbkdf2_sha256.hash(raw)

    @staticmethod
    def verify_password(raw: str, hashed: str) -> bool:
        if not hashed:
            return False
        try:
            return pbkdf2_sha256.verify(raw, hashed)
        except (ValueError, TypeError):
            return False

    def admin_secret_matches(self, secret: str) -> bool:
        modo = self.config.auth_mode
        if modo == "auto":
            return bool(self.config.auto_token) and hmac.compare_digest(secret, self.config.auto_token)
        if modo == "manual":
            return self.verify_password(secret, self.config.password_hash)
        return False

    # Genera un token efímero con identificador único y caducidad.
    def issue(self, role: str) -> tuple[str, int]:
        ahora = datetime.now(timezone.utc)
        expira = ahora + timedelta(seconds=TOKEN_TTL_SECONDS)
        id_token = uuid.uuid4().hex
        carga = {
            "sub": role,
            "role": role,
            "jti": id_token,
            "iat": int(ahora.timestamp()),
            "exp": int(expira.timestamp()),
        }
        token = jwt.encode(carga, self._secret, algorithm=self._algorithm)
        with self._lock:
            self._prune_locked()
            self._active[id_token] = expira.timestamp()
        return token, TOKEN_TTL_SECONDS

    # Valida firma, caducidad y revocación antes de devolver la carga.
    def verify(self, token: str | None) -> dict | None:
        if not token:
            return None
        try:
            carga = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except JWTError:
            return None
        id_token = carga.get("jti")
        with self._lock:
            self._prune_locked()
            if id_token not in self._active:
                return None
        return carga

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        try:
            carga = jwt.decode(token, self._secret, algorithms=[self._algorithm], options={"verify_exp": False})
        except JWTError:
            return
        with self._lock:
            self._active.pop(carga.get("jti", ""), None)

    def _prune_locked(self) -> None:
        ahora = time.time()
        expirado = [id_token for id_token, expiracion in self._active.items() if expiracion <= ahora]
        for id_token in expirado:
            self._active.pop(id_token, None)
