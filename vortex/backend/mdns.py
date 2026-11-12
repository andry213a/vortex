from __future__ import annotations

import socket
import threading

from zeroconf import ServiceInfo, Zeroconf


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


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
                address = local_ip()
                info = ServiceInfo(
                    "_http._tcp.local.",
                    f"Vortex on {socket.gethostname()}._http._tcp.local.",
                    addresses=[socket.inet_aton(address)],
                    port=self.port,
                    properties={
                        "path": "/",
                        "version": "1.0.0",
                        "auth": "vortex",
                    },
                    server=self.hostname,
                )
                zeroconf = Zeroconf()
                zeroconf.register_service(info)
                self._zeroconf = zeroconf
                self._info = info
                self.address = address
                self.error = None
                return True
            except Exception as exc:
                self.error = str(exc)
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
