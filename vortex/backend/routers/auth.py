from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..audit import INFO, SECURITY, WARNING
from ..deps import Access, client_ip, extract_token, get_runtime, require_read
from ..models import AuthStatusResponse, LoginRequest, MeResponse, TokenResponse
from ..state import Runtime

router = APIRouter()


@router.get("/status", response_model=AuthStatusResponse)
def status(runtime: Runtime = Depends(get_runtime)) -> AuthStatusResponse:
    config = runtime.config
    requires_login = config.auth_mode in {"auto", "manual"} and not config.allow_guest
    return AuthStatusResponse(
        auth_mode=config.auth_mode,
        requires_login=requires_login,
        allow_guest=config.allow_guest,
        guest_write=config.guest_write,
        hostname=config.hostname,
        port=config.port,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, runtime: Runtime = Depends(get_runtime)) -> TokenResponse:
    ip = client_ip(request)
    if runtime.rate_limiter.is_blocked(ip):
        remaining = runtime.rate_limiter.remaining_block(ip)
        runtime.audit.log(
            action="RATE_LIMIT_BLOCK",
            severity=SECURITY,
            client_ip=ip,
            user_agent=request.headers.get("user-agent", ""),
            target_path="/api/auth/login",
            status_code=429,
            details={"remaining_seconds": remaining},
        )
        raise HTTPException(status_code=429, detail=f"Demasiados intentos fallidos. Reintente en {remaining} segundos")
    if runtime.config.auth_mode == "none":
        raise HTTPException(status_code=400, detail="La autenticación está desactivada en este servidor")
    if not runtime.auth.admin_secret_matches(payload.secret):
        blocked = runtime.rate_limiter.record_failure(ip)
        runtime.audit.log(
            action="LOGIN_FAILED",
            severity=WARNING,
            client_ip=ip,
            user_agent=request.headers.get("user-agent", ""),
            target_path="/api/auth/login",
            status_code=401,
            details={"blocked": blocked},
        )
        if blocked:
            runtime.audit.log(
                action="RATE_LIMIT_BLOCK",
                severity=SECURITY,
                client_ip=ip,
                user_agent=request.headers.get("user-agent", ""),
                target_path="/api/auth/login",
                status_code=429,
                details={"block_seconds": runtime.rate_limiter.block_seconds},
            )
            raise HTTPException(status_code=429, detail="Cuenta bloqueada temporalmente por demasiados intentos")
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    runtime.rate_limiter.reset(ip)
    token, expires_in = runtime.auth.issue("host")
    runtime.audit.log(
        action="LOGIN_SUCCESS",
        severity=INFO,
        client_ip=ip,
        user_agent=request.headers.get("user-agent", ""),
        target_path="/api/auth/login",
        status_code=200,
        details={"role": "host"},
    )
    return TokenResponse(access_token=token, expires_in=expires_in, role="host")


@router.get("/me", response_model=MeResponse)
def me(access: Access = Depends(require_read), runtime: Runtime = Depends(get_runtime)) -> MeResponse:
    return MeResponse(role=access.role or "guest_read", auth_mode=runtime.config.auth_mode)


@router.post("/logout")
def logout(request: Request, access: Access = Depends(require_read), runtime: Runtime = Depends(get_runtime)) -> dict[str, bool]:
    runtime.auth.revoke(extract_token(request))
    return {"ok": True}
