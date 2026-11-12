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
        mode = self.config.auth_mode
        if mode == "auto":
            return bool(self.config.auto_token) and hmac.compare_digest(secret, self.config.auto_token)
        if mode == "manual":
            return self.verify_password(secret, self.config.password_hash)
        return False

    def issue(self, role: str) -> tuple[str, int]:
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=TOKEN_TTL_SECONDS)
        jti = uuid.uuid4().hex
        payload = {
            "sub": role,
            "role": role,
            "jti": jti,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }
        token = jwt.encode(payload, self._secret, algorithm=self._algorithm)
        with self._lock:
            self._prune_locked()
            self._active[jti] = expires.timestamp()
        return token, TOKEN_TTL_SECONDS

    def verify(self, token: str | None) -> dict | None:
        if not token:
            return None
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm])
        except JWTError:
            return None
        jti = payload.get("jti")
        with self._lock:
            self._prune_locked()
            if jti not in self._active:
                return None
        return payload

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algorithm], options={"verify_exp": False})
        except JWTError:
            return
        with self._lock:
            self._active.pop(payload.get("jti", ""), None)

    def _prune_locked(self) -> None:
        now = time.time()
        expired = [jti for jti, exp in self._active.items() if exp <= now]
        for jti in expired:
            self._active.pop(jti, None)
