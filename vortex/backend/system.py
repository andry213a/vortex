# Utilidades del sistema: puertos, privilegios y disco.

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None


# Comprobaciones de puertos, permisos y espacio en disco.
def port_in_use(port: int) -> bool:
    if psutil is not None:
        try:
            for conexion in psutil.net_connections(kind="inet"):
                if conexion.status == psutil.CONN_LISTEN and conexion.laddr and conexion.laddr.port == port:
                    return True
        except (psutil.Error, OSError):
            pass
    conexion_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            conexion_tcp.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        conexion_tcp.bind(("0.0.0.0", port))
    except OSError:
        return True
    finally:
        conexion_tcp.close()
    return False


def validate_port(port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool):
        raise TypeError("El puerto debe ser un número entero")
    if not 1 <= port <= 65535:
        raise ValueError("El puerto debe estar entre 1 y 65535")
    if port_in_use(port):
        raise ValueError(f"El puerto {port} ya está en uso")


def suggest_free_port(start: int = 8080, attempts: int = 50) -> int:
    for desplazamiento in range(attempts):
        candidato = start + desplazamiento
        if candidato > 65535:
            break
        if not port_in_use(candidato):
            return candidato
    return start


def disk_usage(path: Path) -> dict[str, int]:
    uso = shutil.disk_usage(str(path))
    return {"total": uso.total, "used": uso.used, "free": uso.free}


def is_admin() -> bool:
    if os.name == "nt":
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# Rutas del sistema que no deben compartirse por accidente.
def is_protected_path(path: Path) -> bool:
    try:
        resuelto = path.resolve()
    except OSError:
        return False
    if resuelto == Path(resuelto.anchor):
        return True
    if os.name == "nt":
        raices = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("SystemRoot", r"C:\Windows")]
        for raiz in raices:
            if raiz and resuelto.is_relative_to(Path(raiz)):
                return True
    else:
        protegidas = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev", "/var", "/root", "/opt"]
        for raiz in protegidas:
            if resuelto.is_relative_to(Path(raiz)):
                return True
    return False


def needs_elevation(path: Path) -> bool:
    if is_admin():
        return False
    if is_protected_path(path):
        return True
    return not os.access(str(path), os.W_OK)


# Relanzamiento con privilegios cuando la carpeta lo requiere.
def relaunch_elevated(argv: list[str] | None = None) -> bool:
    argv = list(argv or sys.argv)
    if os.name == "nt":
        try:
            import ctypes

            parametros = subprocess.list2cmdline(argv[1:])
            resultado = ctypes.windll.shell32.ShellExecuteW(None, "runas", argv[0], parametros, None, 1)
            return int(resultado) > 32
        except Exception:
            return False
    comando = [sys.executable, *argv[1:]]
    for lanzador in (["pkexec"], ["sudo", "-E"]):
        if shutil.which(lanzador[0]):
            try:
                os.execvp(lanzador[0], [*lanzador, *comando])
            except OSError:
                continue
    return False


def platform_info() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "hostname": socket.gethostname(),
    }
