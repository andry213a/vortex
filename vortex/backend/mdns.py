# Anuncio y descubrimiento del servidor mediante mDNS.

from __future__ import annotations

import socket
import threading

from zeroconf import ServiceInfo, Zeroconf


def local_ip() -> str:
    conexion_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        conexion_udp.connect(("8.8.8.8", 80))
        return conexion_udp.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        conexion_udp.close()


class MDNSAdvertiser:
    def __init__(self, hostname: str = "vortex.local", port: int = 8080) -> None:
        self.hostname = hostname if hostname.endswith(".") else f"{hostname}."
        self.port = port
        self._zeroconf: Zeroconf | None = None
        self._info: ServiceInfo | None = None
        self._lock = threading.Lock()
        self.error: str | None = None
        self.address: str | None = None

    @property
    def running(self) -> bool:
        return self._zeroconf is not None

    def start(self) -> bool:
        with self._lock:
            if self._zeroconf is not None:
                return True
            try:
                direccion = local_ip()
                informacion = ServiceInfo(
                    "_http._tcp.local.",
                    f"Vortex on {socket.gethostname()}._http._tcp.local.",
                    addresses=[socket.inet_aton(direccion)],
                    port=self.port,
                    properties={
                        "path": "/",
                        "version": "1.0.0",
                        "auth": "vortex",
                    },
                    server=self.hostname,
                )
                servicio_mdns = Zeroconf()
                servicio_mdns.register_service(informacion)
                self._zeroconf = servicio_mdns
                self._info = informacion
                self.address = direccion
                self.error = None
                return True
            except Exception as excepcion:
                self.error = str(excepcion)
                self._zeroconf = None
                self._info = None
                return False

    def stop(self) -> None:
        with self._lock:
            if self._zeroconf is None:
                return
            try:
                if self._info is not None:
                    self._zeroconf.unregister_service(self._info)
            except Exception:
                pass
            try:
                self._zeroconf.close()
            except Exception:
                pass
            self._zeroconf = None
            self._info = None
