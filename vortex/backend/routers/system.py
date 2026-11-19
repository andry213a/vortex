# Rutas de información del sistema y comprobación de puertos.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ... import __version__
from ..deps import Access, get_runtime, require_read
from ..models import DiskInfo, PortCheckResponse, SystemInfoResponse
from ..state import Runtime
from ..system import disk_usage, platform_info, port_in_use, suggest_free_port, validate_port

router = APIRouter()


@router.get("/changes")
def file_changes(
    since: int = Query(0, ge=0),
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> dict:
    return runtime.file_changes_since(since)


@router.get("/info", response_model=SystemInfoResponse)
def info(access: Access = Depends(require_read), runtime: Runtime = Depends(get_runtime)) -> SystemInfoResponse:
    uso = disk_usage(runtime.base_dir)
    configuracion = runtime.config
    return SystemInfoResponse(
        base_dir=str(runtime.base_dir),
        port=configuracion.port,
        auth_mode=configuracion.auth_mode,
        hostname=configuracion.hostname,
        uptime=runtime.uptime(),
        disk=DiskInfo(**uso),
        platform=platform_info(),
        version=__version__,
    )


@router.get("/port-check", response_model=PortCheckResponse)
def port_check(port: int = Query(..., ge=1, le=65535), runtime: Runtime = Depends(get_runtime), access: Access = Depends(require_read)) -> PortCheckResponse:
    if port_in_use(port):
        sugerencia = suggest_free_port(port + 1)
        return PortCheckResponse(port=port, available=False, suggestion=sugerencia, message=f"El puerto {port} está ocupado")
    try:
        validate_port(port)
    except ValueError as excepcion:
        return PortCheckResponse(port=port, available=False, suggestion=suggest_free_port(port + 1), message=str(excepcion))
    return PortCheckResponse(port=port, available=True, message="Puerto disponible")
