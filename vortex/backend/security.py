# Resolución segura de rutas, búsqueda difusa y rate limiting.

from __future__ import annotations

import difflib
import ipaddress
import itertools
import re
import threading
import time
from pathlib import Path

from ..config import LOGIN_BLOCK_SECONDS, LOGIN_MAX_ATTEMPTS

EXT_TYPES: dict[str, str] = {
    "video": {"mp4", "mkv", "avi", "mov", "webm", "m4v", "flv", "wmv", "mpeg", "mpg"},
    "audio": {"mp3", "wav", "flac", "ogg", "m4a", "aac", "opus", "wma"},
    "image": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "avif", "tiff"},
    "text": {"txt", "md", "log", "ini", "cfg"},
    "code": {"py", "js", "ts", "jsx", "tsx", "json", "html", "htm", "css", "xml", "yml", "yaml", "sh", "bat", "ps1", "c", "cpp", "h", "java", "go", "rs", "sql", "toml", "env", "php", "rb", "swift", "kt", "dart", "vue", "svelte"},
    "pdf": {"pdf"},
    "archive": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"},
    "doc": {"doc", "docx", "odt", "rtf"},
    "spreadsheet": {"xls", "xlsx", "xlsm", "ods", "csv", "tsv"},
    "presentation": {"ppt", "pptx", "pptm", "odp", "key"},
    "design": {"psd", "ai", "eps", "indd", "sketch", "fig", "xcf", "afdesign"},
    "font": {"ttf", "otf", "woff", "woff2"},
    "database": {"db", "sqlite", "sqlite3"},
    "model3d": {"glb", "gltf", "obj", "fbx", "stl", "blend"},
    "package": {"deb", "rpm", "appimage", "apk", "dmg", "msi", "pkg"},
    "binary": {"exe", "bin", "so", "dll", "app"},
    "ebook": {"epub", "mobi", "azw", "azw3"},
}
TYPE_BY_EXT: dict[str, str] = {ext: tipo for tipo, extensiones in EXT_TYPES.items() for ext in extensiones}

TEXT_EDITABLE = {"md", "py", "json", "txt", "html", "htm", "css", "js", "ts", "yml", "yaml", "xml", "csv", "log", "ini", "cfg", "sh", "bat", "ps1"}

SIZE_UNITS = {
    "b": 1,
    "kb": 1024,
    "mb": 1024**2,
    "gb": 1024**3,
    "tb": 1024**4,
}


class PathTraversalError(Exception):
    pass


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        direccion = ipaddress.ip_address(host)
    except ValueError:
        return host in {"localhost", "::1"}
    return direccion.is_loopback


# Resolución de rutas confinadas a la carpeta raíz.
def resolve_within(base_dir: Path, relative: str) -> Path:
    if "\x00" in relative:
        raise PathTraversalError("Ruta contiene bytes nulos")
    raiz = Path(base_dir).resolve()
    try:
        candidato = (raiz / relative).resolve()
    except (OSError, RuntimeError) as excepcion:
        raise PathTraversalError(str(excepcion)) from excepcion
    if not candidato.is_relative_to(raiz):
        raise PathTraversalError("La ruta escapa del directorio raíz")
    return candidato


def relative_to(base_dir: Path, target: Path) -> str:
    raiz = Path(base_dir).resolve()
    valor = Path(target).resolve().relative_to(raiz).as_posix()
    return "" if valor == "." else valor


def file_extension(name: str) -> str:
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].lower()


def file_kind(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    return TYPE_BY_EXT.get(file_extension(name), "other")


def is_text_editable(name: str) -> bool:
    return file_extension(name) in TEXT_EDITABLE


# Utilidades de búsqueda y filtros de tamaño.
def parse_size(value: str) -> int | None:
    coincidencia = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", value)
    if not coincidencia:
        return None
    numero = float(coincidencia.group(1))
    unidad = (coincidencia.group(2) or "b").lower()
    if unidad not in SIZE_UNITS:
        return None
    return int(numero * SIZE_UNITS[unidad])


# Puntuación difusa por subsecuencia y similitud.
def _subsequence_score(q: str, t: str) -> float:
    posiciones: list[int] = []
    indice = 0
    for caracter in q:
        encontrado = t.find(caracter, indice)
        if encontrado == -1:
            return 0.0
        posiciones.append(encontrado)
        indice = encontrado + 1
    huecos = sum(fin - inicio - 1 for inicio, fin in itertools.pairwise(posiciones))
    return max(1.0, 25.0 - huecos * 1.5 - (len(t) - len(q)) * 0.05)


def _similarity_score(q: str, t: str) -> float:
    mejor = difflib.SequenceMatcher(None, q, t).ratio()
    tramo = len(q)
    if tramo and len(t) > tramo:
        for inicio in range(len(t) - tramo + 1):
            proporcion = difflib.SequenceMatcher(None, q, t[inicio : inicio + tramo]).ratio()
            mejor = max(mejor, proporcion)
    if mejor < 0.62:
        return 0.0
    return max(1.0, mejor * 30.0)


def fuzzy_score(query: str, text: str) -> float:
    consulta_corta = query.strip().lower()
    texto_corto = text.lower()
    if not consulta_corta:
        return 1.0
    if consulta_corta == texto_corto:
        return 100.0
    if consulta_corta in texto_corto:
        return 50.0 + (len(consulta_corta) / max(len(texto_corto), 1)) * 20.0
    subsecuencia = _subsequence_score(consulta_corta, texto_corto)
    if subsecuencia > 0:
        return subsecuencia
    return _similarity_score(consulta_corta, texto_corto)


# Parser de la sintaxis de búsqueda: type:, ext: y size:.
class SearchQuery:
    def __init__(self, raw: str = "", kind: str = "", ext: str = "", min_size: int | None = None, max_size: int | None = None) -> None:
        self.terms: list[str] = []
        self.kinds: set[str] = set()
        self.exts: set[str] = set()
        self.min_size = min_size
        self.max_size = max_size
        if kind:
            self.kinds.add(kind.lower())
        if ext:
            self.exts.add(ext.lower().lstrip("."))
        self._parse(raw)

    def _parse(self, raw: str) -> None:
        for token in raw.split():
            minusculas = token.lower()
            if minusculas.startswith("type:"):
                self.kinds.add(minusculas.split(":", 1)[1].lstrip("."))
            elif minusculas.startswith("ext:"):
                self.exts.add(minusculas.split(":", 1)[1].lstrip("."))
            elif minusculas.startswith("size:"):
                especificacion = minusculas.split(":", 1)[1]
                operador = ">"
                for prefijo in (">=", "<=", ">", "<", "="):
                    if especificacion.startswith(prefijo):
                        operador = prefijo
                        especificacion = especificacion[len(prefijo):]
                        break
                tamano = parse_size(especificacion)
                if tamano is None:
                    continue
                if operador in (">", ">="):
                    self.min_size = tamano
                elif operador in ("<", "<="):
                    self.max_size = tamano
                else:
                    self.min_size = tamano
                    self.max_size = tamano
            else:
                self.terms.append(token)

    @property
    def text(self) -> str:
        return " ".join(self.terms)

    def matches(self, name: str, is_dir: bool, size: int, ext: str) -> tuple[bool, float]:
        tipo = file_kind(name, is_dir)
        if self.kinds and tipo not in self.kinds:
            return False, 0.0
        if self.exts and ext not in self.exts:
            return False, 0.0
        tiene_filtro_tamano = self.min_size is not None or self.max_size is not None
        if tiene_filtro_tamano:
            if is_dir:
                return False, 0.0
            if self.min_size is not None and size < self.min_size:
                return False, 0.0
            if self.max_size is not None and size > self.max_size:
                return False, 0.0
        if not self.terms:
            return True, 1.0
        mejor = 0.0
        for termino in self.terms:
            puntuacion = fuzzy_score(termino, name)
            if puntuacion <= 0:
                return False, 0.0
            mejor += puntuacion
        return True, mejor / len(self.terms)


# Limitador de intentos de login por dirección IP.
class RateLimiter:
    def __init__(self, max_attempts: int = LOGIN_MAX_ATTEMPTS, block_seconds: int = LOGIN_BLOCK_SECONDS) -> None:
        self.max_attempts = max_attempts
        self.block_seconds = block_seconds
        self._failures: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_blocked(self, ip: str) -> bool:
        with self._lock:
            hasta = self._blocked.get(ip)
            if hasta is None:
                return False
            if hasta <= time.time():
                self._blocked.pop(ip, None)
                self._failures.pop(ip, None)
                return False
            return True

    def remaining_block(self, ip: str) -> int:
        with self._lock:
            hasta = self._blocked.get(ip, 0)
            return max(0, int(hasta - time.time()))

    def record_failure(self, ip: str) -> bool:
        ahora = time.time()
        with self._lock:
            historial = [texto_corto for texto_corto in self._failures.get(ip, []) if ahora - texto_corto < self.block_seconds]
            historial.append(ahora)
            self._failures[ip] = historial
            if len(historial) >= self.max_attempts:
                self._blocked[ip] = ahora + self.block_seconds
                self._failures[ip] = []
                return True
            return False

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._blocked.pop(ip, None)

    def status(self, ip: str) -> dict[str, int | bool]:
        ahora = time.time()
        with self._lock:
            hasta = self._blocked.get(ip, 0)
            bloqueado = hasta > ahora
            historial = [texto_corto for texto_corto in self._failures.get(ip, []) if ahora - texto_corto < self.block_seconds]
            return {
                "blocked": bloqueado,
                "remaining": max(0, int(hasta - ahora)) if bloqueado else 0,
                "attempts": len(historial),
                "max_attempts": self.max_attempts,
            }
