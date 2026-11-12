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


def create_app(config: ServerConfig, runtime: Runtime | None = None) -> FastAPI:
    config.validate()
    runtime = runtime or Runtime(config)
    app = FastAPI(
        title="Vortex",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges", "Content-Length"],
    )
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(system.router, prefix="/api/system", tags=["system"])
    app.include_router(files.router, prefix="/api/files", tags=["files"])
    app.include_router(logs.router, prefix="/api", tags=["logs"])

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    index_file = STATIC_DIR / "index.html"
    static_root = STATIC_DIR.resolve()

    @app.get("/{full_path:path}")
    def spa(request: Request, full_path: str):
        if full_path.startswith("api/") or full_path == "api":
            return JSONResponse({"detail": "No encontrado"}, status_code=404)
        candidate = STATIC_DIR / full_path
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = None
        if resolved is not None and resolved.is_relative_to(static_root) and resolved.is_file():
            return FileResponse(resolved)
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse({"detail": "La interfaz web no está incluida en esta compilación"}, status_code=404)

    return app
