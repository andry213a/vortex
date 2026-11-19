# Control del servidor Uvicorn en un hilo y arranque headless.

from __future__ import annotations

import threading
import time

import uvicorn

from .backend.app import create_app
from .backend.audit import INFO
from .backend.mdns import local_ip
from .backend.state import Runtime
from .backend.system import validate_port
from .config import ServerConfig
from .stdstreams import ensure_standard_streams


class ServerStartError(Exception):
    pass


# Arranque y parada de Uvicorn en un hilo controlado.
class ServerController:
    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.server: uvicorn.Server | None = None
        self.runtime: Runtime | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.server is not None and self.thread is not None and self.thread.is_alive()

    def start(self, config: ServerConfig, wait_timeout: float = 20.0) -> Runtime:
        ensure_standard_streams()
        with self._lock:
            if self.running:
                raise ServerStartError("El servidor ya está en ejecución")
            config.validate()
            validate_port(config.port)
            aplicacion = create_app(config)
            entorno = aplicacion.state.runtime
            config_uvicorn = uvicorn.Config(
                aplicacion,
                host="0.0.0.0",
                port=config.port,
                log_level="warning",
                access_log=False,
                loop="asyncio",
                lifespan="on",
            )
            servidor = uvicorn.Server(config_uvicorn)
            servidor.install_signal_handlers = lambda: None
            hilo = threading.Thread(target=servidor.run, name="vortex-server", daemon=True)
            hilo.start()
            limite = time.monotonic() + wait_timeout
            while time.monotonic() < limite:
                if servidor.started:
                    break
                if not hilo.is_alive():
                    raise ServerStartError("El servidor se detuvo durante el arranque")
                time.sleep(0.05)
            else:
                servidor.should_exit = True
                raise ServerStartError("Tiempo de espera agotado al iniciar el servidor")
            entorno.mdns.start()
            entorno.audit.log(
                action="SERVER_START",
                severity=INFO,
                client_ip=local_ip(),
                target_path=str(entorno.base_dir),
                status_code=200,
                details={"port": config.port, "auth_mode": config.auth_mode, "hostname": config.hostname},
            )
            self.server = servidor
            self.thread = hilo
            self.runtime = entorno
            return entorno

    def stop(self) -> None:
        with self._lock:
            if self.server is None:
                return
            entorno = self.runtime
            if entorno is not None:
                entorno.audit.log(action="SERVER_STOP", severity=INFO, status_code=200)
                entorno.mdns.stop()
            self.server.should_exit = True
            if self.thread is not None:
                self.thread.join(timeout=5)
            self.server = None
            self.thread = None
            self.runtime = None


def run_headless(config: ServerConfig) -> None:
    ensure_standard_streams()
    config.validate()
    validate_port(config.port)
    aplicacion = create_app(config)
    entorno: Runtime = aplicacion.state.runtime
    entorno.mdns.start()
    entorno.audit.log(
        action="SERVER_START",
        severity=INFO,
        client_ip=local_ip(),
        target_path=str(entorno.base_dir),
        status_code=200,
        details={"port": config.port, "auth_mode": config.auth_mode, "hostname": config.hostname},
    )
    try:
        uvicorn.run(aplicacion, host="0.0.0.0", port=config.port, log_level="info", access_log=False, lifespan="on")
    finally:
        entorno.mdns.stop()
        entorno.audit.log(action="SERVER_STOP", severity=INFO, status_code=200)
