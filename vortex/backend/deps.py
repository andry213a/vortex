# Dependencias de acceso por rol y utilidades de autenticación.

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from .audit import SECURITY
from .security import is_loopback
from .state import Runtime


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    reenviado = request.headers.get("x-forwarded-for")
    if reenviado:
        return reenviado.split(",")[0].strip()
    return ""


def extract_token(request: Request) -> str | None:
    cabecera = request.headers.get("authorization", "")
    if cabecera.lower().startswith("bearer "):
        token = cabecera[7:].strip()
        if token:
            return token
    token = request.query_params.get("token")
    return token or None


@dataclass
class Access:
    role: str | None
    ip: str


def current_access(request: Request) -> Access:
    entorno = get_runtime(request)
    ip = client_ip(request)
    rol: str | None = None
    token = extract_token(request)
    if token:
        carga = entorno.auth.verify(token)
        if carga:
            rol = str(carga.get("role") or "") or None
    if rol is None:
        configuracion = entorno.config
        if configuracion.auth_mode == "none":
            if is_loopback(ip):
                rol = "host"
            else:
                rol = "guest_write" if configuracion.guest_write else "guest_read"
        elif configuracion.allow_guest:
            rol = "guest_write" if configuracion.guest_write else "guest_read"
    return Access(role=rol, ip=ip)


def _audit_denied(request: Request, runtime: Runtime, access: Access, status_code: int, reason: str) -> None:
    runtime.audit.log(
        action="UNAUTHORIZED",
        severity=SECURITY,
        client_ip=access.ip,
        user_agent=request.headers.get("user-agent", ""),
        target_path=request.url.path,
        status_code=status_code,
        details={"method": request.method, "reason": reason},
    )


def require_read(request: Request, access: Access = Depends(current_access)) -> Access:
    if access.role is None:
        entorno = get_runtime(request)
        _audit_denied(request, entorno, access, 401, "missing_or_invalid_token")
        raise HTTPException(status_code=401, detail="No autenticado", headers={"WWW-Authenticate": "Bearer"})
    return access


def require_write(request: Request, access: Access = Depends(require_read)) -> Access:
    if access.role not in {"host", "guest_write"}:
        entorno = get_runtime(request)
        _audit_denied(request, entorno, access, 403, "read_only")
        raise HTTPException(status_code=403, detail="Permisos de escritura insuficientes")
    return access


def require_host(request: Request, access: Access = Depends(require_read)) -> Access:
    if access.role != "host":
        entorno = get_runtime(request)
        _audit_denied(request, entorno, access, 403, "host_only")
        raise HTTPException(status_code=403, detail="Solo el host puede acceder a este recurso")
    return access
