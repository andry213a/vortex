# Subidas directas y por bloques, rangos HTTP y ensamblado.

from __future__ import annotations

import json
import math
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

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


# Comprobaciones de espacio y nombres únicos antes de escribir.
def ensure_free_space(path: Path, required: int, margin: int = DISK_MARGIN_BYTES) -> None:
    if required <= 0:
        return
    libre = shutil.disk_usage(str(path)).free
    if libre < required + margin:
        raise InsufficientStorage(
            f"Espacio insuficiente en disco: se necesitan {required + margin} bytes y hay {libre}"
        )


def unique_path(directory: Path, filename: str) -> Path:
    nombre = Path(filename).name.strip()
    if not nombre or nombre in {".", ".."}:
        raise UploadError("Nombre de archivo inválido")
    candidato = directory / nombre
    if not candidato.exists():
        return candidato
    tronco = Path(nombre).stem
    sufijo = Path(nombre).suffix
    contador = 1
    while True:
        candidato = directory / f"{tronco}_{contador}{sufijo}"
        if not candidato.exists():
            return candidato
        contador += 1


# Interpretación de cabeceras Range y lectura por bloques.
def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    valor = header.strip()
    if not valor.lower().startswith("bytes="):
        return None
    especificacion = valor[6:].split(",")[0].strip()
    if "-" not in especificacion:
        return None
    texto_inicio, _, texto_fin = especificacion.partition("-")
    if texto_inicio == "":
        if not texto_fin:
            return None
        longitud = int(texto_fin)
        if longitud <= 0:
            raise ValueError("Rango inválido")
        inicio = max(0, size - longitud)
        fin = size - 1
    else:
        inicio = int(texto_inicio)
        fin = int(texto_fin) if texto_fin else size - 1
    fin = min(fin, size - 1)
    if inicio < 0 or inicio > fin or inicio >= size:
        raise ValueError("Rango fuera de límites")
    return inicio, fin


def iter_file(path: Path, start: int = 0, end: int | None = None, block_size: int = 1024 * 1024) -> Iterator[bytes]:
    if end is None:
        end = path.stat().st_size - 1
    restante = end - start + 1
    with open(path, "rb") as manejador:
        manejador.seek(start)
        while restante > 0:
            bloque = manejador.read(min(block_size, restante))
            if not bloque:
                break
            restante -= len(bloque)
            yield bloque


# Gestión de subidas por bloques: metadatos, partes y ensamblado.
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
        ruta_meta = self._meta_path(upload_id)
        if not ruta_meta.exists():
            raise UploadNotFound("La subida no existe o expiró")
        try:
            return json.loads(ruta_meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as excepcion:
            raise UploadError("Metadatos de subida corruptos") from excepcion

    def _save(self, upload_id: str, meta: dict) -> None:
        ruta_meta = self._meta_path(upload_id)
        temporal = ruta_meta.with_suffix(".json.tmp")
        temporal.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        temporal.replace(ruta_meta)

    def _received(self, upload_id: str, count: int) -> list[int]:
        directorio = self._upload_dir(upload_id)
        recibidos = [indice for indice in range(count) if (directorio / f"{indice}.part").exists()]
        return recibidos

    # Prepara una subida nueva y calcula el número de bloques.
    def init(self, filename: str, size: int, target_dir: str, chunk_size: int | None = None) -> dict:
        if size < 0:
            raise UploadError("Tamaño de archivo inválido")
        try:
            destino = resolve_within(self.base_dir, target_dir)
        except PathTraversalError as excepcion:
            raise UploadError(str(excepcion)) from excepcion
        if not destino.is_dir():
            raise UploadError("La carpeta destino no existe")
        nombre = Path(filename).name.strip()
        if not nombre or nombre in {".", ".."}:
            raise UploadError("Nombre de archivo inválido")
        try:
            mismo_dispositivo = destino.stat().st_dev == self.root.stat().st_dev
        except OSError:
            mismo_dispositivo = False
        ensure_free_space(destino, size * 2 if mismo_dispositivo else size)
        if not mismo_dispositivo:
            ensure_free_space(self.root, size)
        bloque = int(chunk_size or self.chunk_size)
        if bloque < 256 * 1024:
            bloque = self.chunk_size
        cantidad = max(1, math.ceil(size / bloque)) if size else 1
        id_subida = uuid.uuid4().hex
        directorio = self._upload_dir(id_subida)
        directorio.mkdir(parents=True, exist_ok=True)
        metadatos = {
            "upload_id": id_subida,
            "filename": nombre,
            "size": int(size),
            "chunk_size": bloque,
            "chunk_count": cantidad,
            "target_dir": target_dir,
            "created_at": time.time(),
        }
        self._save(id_subida, metadatos)
        return {**metadatos, "received": []}

    def status(self, upload_id: str) -> dict:
        metadatos = self._load(upload_id)
        recibidos = self._received(upload_id, metadatos["chunk_count"])
        return {**metadatos, "received": recibidos}

    # Guarda un bloque de forma atómica antes de marcarlo como recibido.
    def save_chunk(self, upload_id: str, index: int, data: bytes) -> dict:
        metadatos = self._load(upload_id)
        if index < 0 or index >= metadatos["chunk_count"]:
            raise UploadError("Índice de bloque fuera de rango")
        esperado = metadatos["chunk_size"]
        if index == metadatos["chunk_count"] - 1 and metadatos["size"]:
            esperado = metadatos["size"] - metadatos["chunk_size"] * (metadatos["chunk_count"] - 1)
        if len(data) > esperado:
            raise UploadError("Bloque de subida demasiado grande")
        directorio = self._upload_dir(upload_id)
        parte = directorio / f"{index}.part"
        temporal = directorio / f"{index}.part.tmp"
        temporal.write_bytes(data)
        temporal.replace(parte)
        recibidos = self._received(upload_id, metadatos["chunk_count"])
        return {
            "upload_id": upload_id,
            "received": recibidos,
            "received_count": len(recibidos),
            "chunk_count": metadatos["chunk_count"],
        }

    # Verifica que no falten bloques y ensambla el archivo final.
    def complete(self, upload_id: str) -> Path:
        metadatos = self._load(upload_id)
        recibidos = self._received(upload_id, metadatos["chunk_count"])
        faltantes = [indice for indice in range(metadatos["chunk_count"]) if indice not in recibidos]
        if faltantes:
            raise IncompleteUpload(faltantes)
        directorio = self._upload_dir(upload_id)
        try:
            directorio_destino = resolve_within(self.base_dir, metadatos["target_dir"])
        except PathTraversalError as excepcion:
            raise UploadError(str(excepcion)) from excepcion
        destino = unique_path(directorio_destino, metadatos["filename"])
        destino_temporal = destino.with_name(f"{destino.name}.vortex-assemble")
        try:
            with open(destino_temporal, "wb") as salida:
                for indice in range(metadatos["chunk_count"]):
                    parte = directorio / f"{indice}.part"
                    with open(parte, "rb") as origen:
                        shutil.copyfileobj(origen, salida, length=1024 * 1024)
            total = destino_temporal.stat().st_size
            if metadatos["size"] and total != metadatos["size"]:
                raise UploadError("El tamaño final no coincide con el esperado")
            destino_temporal.replace(destino)
        except BaseException:
            destino_temporal.unlink(missing_ok=True)
            raise
        self.abort(upload_id)
        return destino

    def abort(self, upload_id: str) -> None:
        directorio = self._upload_dir(upload_id)
        if directorio.exists():
            shutil.rmtree(directorio, ignore_errors=True)

    def cleanup_stale(self, max_age_seconds: int = 24 * 60 * 60) -> int:
        eliminados = 0
        umbral = time.time() - max_age_seconds
        for hijo in self.root.iterdir():
            if not hijo.is_dir() or not UPLOAD_ID_RE.fullmatch(hijo.name):
                continue
            try:
                if hijo.stat().st_mtime < umbral:
                    shutil.rmtree(hijo, ignore_errors=True)
                    eliminados += 1
            except OSError:
                continue
        return eliminados
