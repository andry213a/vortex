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


class ServerStartError(Exception):
    pass


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
        with self._lock:
            if self.running:
                raise ServerStartError("El servidor ya está en ejecución")
            config.validate()
            validate_port(config.port)
            app = create_app(config)
            runtime = app.state.runtime
            uvicorn_config = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=config.port,
                log_level="warning",
                access_log=False,
                loop="asyncio",
                lifespan="on",
            )
            server = uvicorn.Server(uvicorn_config)
            server.install_signal_handlers = lambda: None
            thread = threading.Thread(target=server.run, name="vortex-server", daemon=True)
            thread.start()
            deadline = time.monotonic() + wait_timeout
            while time.monotonic() < deadline:
                if server.started:
                    break
                if not thread.is_alive():
                    raise ServerStartError("El servidor se detuvo durante el arranque")
                time.sleep(0.05)
            else:
                server.should_exit = True
                raise ServerStartError("Tiempo de espera agotado al iniciar el servidor")
            runtime.mdns.start()
            runtime.audit.log(
                action="SERVER_START",
                severity=INFO,
                client_ip=local_ip(),
                target_path=str(runtime.base_dir),
                status_code=200,
                details={"port": config.port, "auth_mode": config.auth_mode, "hostname": config.hostname},
            )
            self.server = server
            self.thread = thread
            self.runtime = runtime
            return runtime

    def stop(self) -> None:
        with self._lock:
            if self.server is None:
                return
            runtime = self.runtime
            if runtime is not None:
                runtime.audit.log(action="SERVER_STOP", severity=INFO, status_code=200)
                runtime.mdns.stop()
            self.server.should_exit = True
            if self.thread is not None:
                self.thread.join(timeout=5)
            self.server = None
            self.thread = None
            self.runtime = None


def run_headless(config: ServerConfig) -> None:
    config.validate()
    validate_port(config.port)
    app = create_app(config)
    runtime: Runtime = app.state.runtime
    runtime.mdns.start()
    runtime.audit.log(
        action="SERVER_START",
        severity=INFO,
        client_ip=local_ip(),
        target_path=str(runtime.base_dir),
        status_code=200,
        details={"port": config.port, "auth_mode": config.auth_mode, "hostname": config.hostname},
    )
    try:
        uvicorn.run(app, host="0.0.0.0", port=config.port, log_level="info", access_log=False, lifespan="on")
    finally:
        runtime.mdns.stop()
        runtime.audit.log(action="SERVER_STOP", severity=INFO, status_code=200)
