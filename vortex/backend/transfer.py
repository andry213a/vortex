from __future__ import annotations

import json
import math
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Iterator

from .security import PathTraversalError, resolve_within

UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class UploadError(Exception):
    pass


class UploadNotFound(UploadError):
    pass


class IncompleteUpload(UploadError):
    def __init__(self, missing: list[int]) -> None:
        super().__init__("Subida incompleta")
        self.missing = missing


class InsufficientStorage(UploadError):
    pass


DISK_MARGIN_BYTES = 32 * 1024 * 1024


def ensure_free_space(path: Path, required: int, margin: int = DISK_MARGIN_BYTES) -> None:
    if required <= 0:
        return
    free = shutil.disk_usage(str(path)).free
    if free < required + margin:
        raise InsufficientStorage(
            f"Espacio insuficiente en disco: se necesitan {required + margin} bytes y hay {free}"
        )


def unique_path(directory: Path, filename: str) -> Path:
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        raise UploadError("Nombre de archivo inválido")
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_text, _, end_text = spec.partition("-")
    if start_text == "":
        if not end_text:
            return None
        length = int(end_text)
        if length <= 0:
            raise ValueError("Rango inválido")
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    end = min(end, size - 1)
    if start < 0 or start > end or start >= size:
        raise ValueError("Rango fuera de límites")
    return start, end


def iter_file(path: Path, start: int = 0, end: int | None = None, block_size: int = 1024 * 1024) -> Iterator[bytes]:
    if end is None:
        end = path.stat().st_size - 1
    remaining = end - start + 1
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(block_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


class UploadManager:
    def __init__(self, uploads_dir: Path, base_dir: Path, chunk_size: int) -> None:
        self.root = Path(uploads_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_dir = Path(base_dir).resolve()
        self.chunk_size = chunk_size

    def _upload_dir(self, upload_id: str) -> Path:
        if not UPLOAD_ID_RE.fullmatch(upload_id or ""):
            raise UploadNotFound("Identificador de subida inválido")
        return self.root / upload_id

    def _meta_path(self, upload_id: str) -> Path:
        return self._upload_dir(upload_id) / "meta.json"

    def _load(self, upload_id: str) -> dict:
        meta_path = self._meta_path(upload_id)
        if not meta_path.exists():
            raise UploadNotFound("La subida no existe o expiró")
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UploadError("Metadatos de subida corruptos") from exc

    def _save(self, upload_id: str, meta: dict) -> None:
        meta_path = self._meta_path(upload_id)
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(meta_path)

    def _received(self, upload_id: str, count: int) -> list[int]:
        directory = self._upload_dir(upload_id)
        received = [index for index in range(count) if (directory / f"{index}.part").exists()]
        return received

    def init(self, filename: str, size: int, target_dir: str, chunk_size: int | None = None) -> dict:
        if size < 0:
            raise UploadError("Tamaño de archivo inválido")
        try:
            destination = resolve_within(self.base_dir, target_dir)
        except PathTraversalError as exc:
            raise UploadError(str(exc)) from exc
        if not destination.is_dir():
            raise UploadError("La carpeta destino no existe")
        name = Path(filename).name.strip()
        if not name or name in {".", ".."}:
            raise UploadError("Nombre de archivo inválido")
        try:
            same_device = destination.stat().st_dev == self.root.stat().st_dev
        except OSError:
            same_device = False
        ensure_free_space(destination, size * 2 if same_device else size)
        if not same_device:
            ensure_free_space(self.root, size)
        chunk = int(chunk_size or self.chunk_size)
        if chunk < 256 * 1024:
            chunk = self.chunk_size
        count = max(1, math.ceil(size / chunk)) if size else 1
        upload_id = uuid.uuid4().hex
        directory = self._upload_dir(upload_id)
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "upload_id": upload_id,
            "filename": name,
            "size": int(size),
            "chunk_size": chunk,
            "chunk_count": count,
            "target_dir": target_dir,
            "created_at": time.time(),
        }
        self._save(upload_id, meta)
        return {**meta, "received": []}

    def status(self, upload_id: str) -> dict:
        meta = self._load(upload_id)
        received = self._received(upload_id, meta["chunk_count"])
        return {**meta, "received": received}

    def save_chunk(self, upload_id: str, index: int, data: bytes) -> dict:
        meta = self._load(upload_id)
        if index < 0 or index >= meta["chunk_count"]:
            raise UploadError("Índice de bloque fuera de rango")
        expected = meta["chunk_size"]
        if index == meta["chunk_count"] - 1 and meta["size"]:
            expected = meta["size"] - meta["chunk_size"] * (meta["chunk_count"] - 1)
        if len(data) > expected:
            raise UploadError("Bloque de subida demasiado grande")
        directory = self._upload_dir(upload_id)
        part = directory / f"{index}.part"
        tmp = directory / f"{index}.part.tmp"
        tmp.write_bytes(data)
        tmp.replace(part)
        received = self._received(upload_id, meta["chunk_count"])
        return {
            "upload_id": upload_id,
            "received": received,
            "received_count": len(received),
            "chunk_count": meta["chunk_count"],
        }

    def complete(self, upload_id: str) -> Path:
        meta = self._load(upload_id)
        received = self._received(upload_id, meta["chunk_count"])
        missing = [index for index in range(meta["chunk_count"]) if index not in received]
        if missing:
            raise IncompleteUpload(missing)
        directory = self._upload_dir(upload_id)
        try:
            destination_dir = resolve_within(self.base_dir, meta["target_dir"])
        except PathTraversalError as exc:
            raise UploadError(str(exc)) from exc
        destination = unique_path(destination_dir, meta["filename"])
        temp_destination = destination.with_name(f"{destination.name}.vortex-assemble")
        try:
            with open(temp_destination, "wb") as output:
                for index in range(meta["chunk_count"]):
                    part = directory / f"{index}.part"
                    with open(part, "rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
            total = temp_destination.stat().st_size
            if meta["size"] and total != meta["size"]:
                raise UploadError("El tamaño final no coincide con el esperado")
            temp_destination.replace(destination)
        except BaseException:
            temp_destination.unlink(missing_ok=True)
            raise
        self.abort(upload_id)
        return destination

    def abort(self, upload_id: str) -> None:
        directory = self._upload_dir(upload_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

    def cleanup_stale(self, max_age_seconds: int = 24 * 60 * 60) -> int:
        removed = 0
        threshold = time.time() - max_age_seconds
        for child in self.root.iterdir():
            if not child.is_dir() or not UPLOAD_ID_RE.fullmatch(child.name):
                continue
            try:
                if child.stat().st_mtime < threshold:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
        return removed
