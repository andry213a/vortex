# Rutas de login, estado de autenticación y sesión.

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..audit import INFO, SECURITY, WARNING
from ..deps import Access, client_ip, extract_token, get_runtime, require_read
from ..models import AuthStatusResponse, LoginRequest, MeResponse, TokenResponse
from ..state import Runtime

router = APIRouter()


@router.get("/status", response_model=AuthStatusResponse)
# Estado público de autenticación para decidir el modo de acceso.
def status(runtime: Runtime = Depends(get_runtime)) -> AuthStatusResponse:
    configuracion = runtime.config
    requiere_inicio_sesion = configuracion.auth_mode in {"auto", "manual"} and not configuracion.allow_guest
    return AuthStatusResponse(
        auth_mode=configuracion.auth_mode,
        requires_login=requiere_inicio_sesion,
        allow_guest=configuracion.allow_guest,
        guest_write=configuracion.guest_write,
        hostname=configuracion.hostname,
        port=configuracion.port,
    )


@router.post("/login", response_model=TokenResponse)
# Validación de contraseña, rate limiting y emisión del token.
def login(payload: LoginRequest, request: Request, runtime: Runtime = Depends(get_runtime)) -> TokenResponse:
    ip = client_ip(request)
    if runtime.rate_limiter.is_blocked(ip):
        restante = runtime.rate_limiter.remaining_block(ip)
        runtime.audit.log(
            action="RATE_LIMIT_BLOCK",
            severity=SECURITY,
            client_ip=ip,
            user_agent=request.headers.get("user-agent", ""),
            target_path="/api/auth/login",
            status_code=429,
            details={"remaining_seconds": restante},
        )
        raise HTTPException(status_code=429, detail=f"Demasiados intentos fallidos. Reintente en {restante} segundos")
    if runtime.config.auth_mode == "none":
        raise HTTPException(status_code=400, detail="La autenticación está desactivada en este servidor")
    if not runtime.auth.admin_secret_matches(payload.secret):
        bloqueado = runtime.rate_limiter.record_failure(ip)
        runtime.audit.log(
            action="LOGIN_FAILED",
            severity=WARNING,
            client_ip=ip,
            user_agent=request.headers.get("user-agent", ""),
            target_path="/api/auth/login",
            status_code=401,
            details={"blocked": bloqueado},
        )
        if bloqueado:
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
    token, segundos_expiracion = runtime.auth.issue("host")
    runtime.audit.log(
        action="LOGIN_SUCCESS",
        severity=INFO,
        client_ip=ip,
        user_agent=request.headers.get("user-agent", ""),
        target_path="/api/auth/login",
        status_code=200,
        details={"role": "host"},
    )
    return TokenResponse(access_token=token, expires_in=segundos_expiracion, role="host")


@router.get("/me", response_model=MeResponse)
def me(access: Access = Depends(require_read), runtime: Runtime = Depends(get_runtime)) -> MeResponse:
    return MeResponse(role=access.role or "guest_read", auth_mode=runtime.config.auth_mode)


@router.post("/logout")
def logout(request: Request, access: Access = Depends(require_read), runtime: Runtime = Depends(get_runtime)) -> dict[str, bool]:
    runtime.auth.revoke(extract_token(request))
    return {"ok": True}
