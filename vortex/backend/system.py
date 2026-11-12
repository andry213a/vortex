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


def port_in_use(port: int) -> bool:
    if psutil is not None:
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                    return True
        except (psutil.Error, OSError):
            pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError:
        return True
    finally:
        sock.close()
    return False


def validate_port(port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("El puerto debe ser un número entero")
    if not 1 <= port <= 65535:
        raise ValueError("El puerto debe estar entre 1 y 65535")
    if port_in_use(port):
        raise ValueError(f"El puerto {port} ya está en uso")


def suggest_free_port(start: int = 8080, attempts: int = 50) -> int:
    for offset in range(attempts):
        candidate = start + offset
        if candidate > 65535:
            break
        if not port_in_use(candidate):
            return candidate
    return start


def disk_usage(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(str(path))
    return {"total": usage.total, "used": usage.used, "free": usage.free}


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


def is_protected_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if resolved == Path(resolved.anchor):
        return True
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("SystemRoot", r"C:\Windows")]
        for root in roots:
            if root and resolved.is_relative_to(Path(root)):
                return True
    else:
        protected = ["/etc", "/usr", "/bin", "/sbin", "/boot", "/sys", "/proc", "/dev", "/var", "/root", "/opt"]
        for root in protected:
            if resolved.is_relative_to(Path(root)):
                return True
    return False


def needs_elevation(path: Path) -> bool:
    if is_admin():
        return False
    if is_protected_path(path):
        return True
    return not os.access(str(path), os.W_OK)


def relaunch_elevated(argv: list[str] | None = None) -> bool:
    argv = list(argv or sys.argv)
    if os.name == "nt":
        try:
            import ctypes

            params = subprocess.list2cmdline(argv[1:])
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", argv[0], params, None, 1)
            return int(result) > 32
        except Exception:
            return False
    command = [sys.executable, *argv[1:]]
    for launcher in (["pkexec"], ["sudo", "-E"]):
        if shutil.which(launcher[0]):
            try:
                os.execvp(launcher[0], [*launcher, *command])
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
