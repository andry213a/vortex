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
TYPE_BY_EXT: dict[str, str] = {ext: kind for kind, exts in EXT_TYPES.items() for ext in exts}

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
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host in {"localhost", "::1"}
    return addr.is_loopback


def resolve_within(base_dir: Path, relative: str) -> Path:
    if "\x00" in relative:
        raise PathTraversalError("Ruta contiene bytes nulos")
    base = Path(base_dir).resolve()
    try:
        candidate = (base / relative).resolve()
    except (OSError, RuntimeError) as exc:
        raise PathTraversalError(str(exc)) from exc
    if not candidate.is_relative_to(base):
        raise PathTraversalError("La ruta escapa del directorio raíz")
    return candidate


def relative_to(base_dir: Path, target: Path) -> str:
    base = Path(base_dir).resolve()
    value = Path(target).resolve().relative_to(base).as_posix()
    return "" if value == "." else value


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


def parse_size(value: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", value)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    if unit not in SIZE_UNITS:
        return None
    return int(number * SIZE_UNITS[unit])


def _subsequence_score(q: str, t: str) -> float:
    positions: list[int] = []
    index = 0
    for char in q:
        found = t.find(char, index)
        if found == -1:
            return 0.0
        positions.append(found)
        index = found + 1
    gaps = sum(b - a - 1 for a, b in itertools.pairwise(positions))
    return max(1.0, 25.0 - gaps * 1.5 - (len(t) - len(q)) * 0.05)


def _similarity_score(q: str, t: str) -> float:
    best = difflib.SequenceMatcher(None, q, t).ratio()
    span = len(q)
    if span and len(t) > span:
        for start in range(len(t) - span + 1):
            ratio = difflib.SequenceMatcher(None, q, t[start : start + span]).ratio()
            best = max(best, ratio)
    if best < 0.62:
        return 0.0
    return max(1.0, best * 30.0)


def fuzzy_score(query: str, text: str) -> float:
    q = query.strip().lower()
    t = text.lower()
    if not q:
        return 1.0
    if q == t:
        return 100.0
    if q in t:
        return 50.0 + (len(q) / max(len(t), 1)) * 20.0
    subsequence = _subsequence_score(q, t)
    if subsequence > 0:
        return subsequence
    return _similarity_score(q, t)


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
            lowered = token.lower()
            if lowered.startswith("type:"):
                self.kinds.add(lowered.split(":", 1)[1].lstrip("."))
            elif lowered.startswith("ext:"):
                self.exts.add(lowered.split(":", 1)[1].lstrip("."))
            elif lowered.startswith("size:"):
                spec = lowered.split(":", 1)[1]
                operator = ">"
                for prefix in (">=", "<=", ">", "<", "="):
                    if spec.startswith(prefix):
                        operator = prefix
                        spec = spec[len(prefix):]
                        break
                size = parse_size(spec)
                if size is None:
                    continue
                if operator in (">", ">="):
                    self.min_size = size
                elif operator in ("<", "<="):
                    self.max_size = size
                else:
                    self.min_size = size
                    self.max_size = size
            else:
                self.terms.append(token)

    @property
    def text(self) -> str:
        return " ".join(self.terms)

    def matches(self, name: str, is_dir: bool, size: int, ext: str) -> tuple[bool, float]:
        kind = file_kind(name, is_dir)
        if self.kinds and kind not in self.kinds:
            return False, 0.0
        if self.exts and ext not in self.exts:
            return False, 0.0
        has_size_filter = self.min_size is not None or self.max_size is not None
        if has_size_filter:
            if is_dir:
                return False, 0.0
            if self.min_size is not None and size < self.min_size:
                return False, 0.0
            if self.max_size is not None and size > self.max_size:
                return False, 0.0
        if not self.terms:
            return True, 1.0
        best = 0.0
        for term in self.terms:
            score = fuzzy_score(term, name)
            if score <= 0:
                return False, 0.0
            best += score
        return True, best / len(self.terms)


class RateLimiter:
    def __init__(self, max_attempts: int = LOGIN_MAX_ATTEMPTS, block_seconds: int = LOGIN_BLOCK_SECONDS) -> None:
        self.max_attempts = max_attempts
        self.block_seconds = block_seconds
        self._failures: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_blocked(self, ip: str) -> bool:
        with self._lock:
            until = self._blocked.get(ip)
            if until is None:
                return False
            if until <= time.time():
                self._blocked.pop(ip, None)
                self._failures.pop(ip, None)
                return False
            return True

    def remaining_block(self, ip: str) -> int:
        with self._lock:
            until = self._blocked.get(ip, 0)
            return max(0, int(until - time.time()))

    def record_failure(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            history = [t for t in self._failures.get(ip, []) if now - t < self.block_seconds]
            history.append(now)
            self._failures[ip] = history
            if len(history) >= self.max_attempts:
                self._blocked[ip] = now + self.block_seconds
                self._failures[ip] = []
                return True
            return False

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._blocked.pop(ip, None)

    def status(self, ip: str) -> dict[str, int | bool]:
        now = time.time()
        with self._lock:
            until = self._blocked.get(ip, 0)
            blocked = until > now
            history = [t for t in self._failures.get(ip, []) if now - t < self.block_seconds]
            return {
                "blocked": blocked,
                "remaining": max(0, int(until - now)) if blocked else 0,
                "attempts": len(history),
                "max_attempts": self.max_attempts,
            }
