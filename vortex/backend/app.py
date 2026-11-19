# Fábrica de la aplicación FastAPI y montaje de routers y SPA.

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import ServerConfig
from ..resources import static_dir
from .routers import auth, files, logs, system
from .state import Runtime

STATIC_DIR = static_dir()


# Construcción de FastAPI, routers, estáticos y fallback de la SPA.
def create_app(config: ServerConfig, runtime: Runtime | None = None) -> FastAPI:
    config.validate()
    runtime = runtime or Runtime(config)
    aplicacion = FastAPI(
        title="Vortex",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    aplicacion.state.runtime = runtime
    aplicacion.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges", "Content-Length"],
    )
    aplicacion.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    aplicacion.include_router(system.router, prefix="/api/system", tags=["system"])
    aplicacion.include_router(files.router, prefix="/api/files", tags=["files"])
    aplicacion.include_router(logs.router, prefix="/api", tags=["logs"])

    if STATIC_DIR.exists():
        aplicacion.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @aplicacion.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    archivo_indice = STATIC_DIR / "index.html"
    raiz_estatica = STATIC_DIR.resolve()

    @aplicacion.get("/{full_path:path}")
    def spa(request: Request, full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            return JSONResponse({"detail": "No encontrado"}, status_code=404)
        candidato = STATIC_DIR / full_path
        try:
            resuelto = candidato.resolve()
        except OSError:
            resuelto = None
        if resuelto is not None and resuelto.is_relative_to(raiz_estatica) and resuelto.is_file():
            return FileResponse(resuelto)
        if archivo_indice.exists():
            return FileResponse(archivo_indice)
        return JSONResponse({"detail": "La interfaz web no está incluida en esta compilación"}, status_code=404)

    return aplicacion
