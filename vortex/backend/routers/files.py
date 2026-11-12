from __future__ import annotations

import mimetypes
import hashlib
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from zipstream import ZipStream

from ..audit import INFO, SECURITY
from ..deps import Access, client_ip, get_runtime, require_read, require_write
from ..models import (
    FileItem,
    FileReadResponse,
    ListResponse,
    MkdirRequest,
    RenameRequest,
    SearchResponse,
    UploadChunkResponse,
    UploadCompleteResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadStatusResponse,
    WriteRequest,
)
from ..security import (
    PathTraversalError,
    SearchQuery,
    file_extension,
    file_kind,
    is_text_editable,
)
from ..state import Runtime
from ..transfer import (
    IncompleteUpload,
    InsufficientStorage,
    UploadError,
    UploadNotFound,
    ensure_free_space,
    iter_file,
    parse_range,
)

router = APIRouter()

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 5 * 1024 * 1024
FORCE_ATTACHMENT_EXTS = {"html", "htm", "svg", "xml", "js", "mjs", "xhtml"}


def _resolve(runtime: Runtime, request: Request, path: str) -> Path:
    try:
        return runtime.resolve(path)
    except PathTraversalError as exc:
        runtime.audit.log(
            action="PATH_TRAVERSAL",
            severity=SECURITY,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            target_path=path,
            status_code=403,
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=403, detail="Ruta no permitida") from exc


def _entry_is_safe(runtime: Runtime, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(runtime.base_dir)
    except (OSError, RuntimeError):
        return False


def _make_item(runtime: Runtime, path: Path) -> FileItem:
    stat = path.stat()
    is_dir = path.is_dir()
    name = path.name
    extension = file_extension(name)
    return FileItem(
        name=name,
        path=runtime.relative(path),
        is_dir=is_dir,
        size=0 if is_dir else stat.st_size,
        modified=stat.st_mtime,
        ext=extension,
        kind=file_kind(name, is_dir),
        editable=(not is_dir and is_text_editable(name)),
    )


def _content_disposition(name: str, inline: bool) -> str:
    disposition = "inline" if inline else "attachment"
    cleaned = "".join(char for char in name if char.isprintable() and char not in '"\\') or "archivo"
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii") or "archivo"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(cleaned)}"


def _audit(runtime: Runtime, request: Request, action: str, path: str, status_code: int, details: dict | None = None) -> None:
    runtime.audit.log(
        action=action,
        severity=INFO,
        client_ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        target_path=path,
        status_code=status_code,
        details=details or {},
    )


@router.get("", response_model=ListResponse)
@router.get("/", response_model=ListResponse)
def list_directory(
    request: Request,
    path: str = "",
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> ListResponse:
    target = _resolve(runtime, request, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="La ruta no es un directorio")
    items: list[FileItem] = []
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                candidate = Path(entry.path)
                if not _entry_is_safe(runtime, candidate):
                    continue
                try:
                    items.append(_make_item(runtime, candidate))
                except OSError:
                    continue
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo leer el directorio: {exc}") from exc
    items.sort(key=lambda item: (not item.is_dir, item.name.lower()))
    parent = None if target == runtime.base_dir else runtime.relative(target.parent)
    return ListResponse(path=runtime.relative(target), parent=parent, items=items, total=len(items))


@router.get("/search", response_model=SearchResponse)
def search(
    request: Request,
    path: str = "",
    q: str = "",
    kind: str = Query("", alias="type"),
    ext: str = "",
    min_size: int | None = Query(None, ge=0),
    max_size: int | None = Query(None, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> SearchResponse:
    root = _resolve(runtime, request, path)
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail="La carpeta de búsqueda no existe")
    query = SearchQuery(q, kind, ext, min_size, max_size)
    results: list[tuple[float, FileItem]] = []
    scanned = 0
    truncated = False
    deadline = time.monotonic() + 6.0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            scanned += 1
            if scanned > 25000 or time.monotonic() > deadline:
                truncated = True
                break
            candidate = Path(dirpath) / name
            if not _entry_is_safe(runtime, candidate):
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            is_dir = candidate.is_dir()
            extension = file_extension(name)
            matched, score = query.matches(name, is_dir, 0 if is_dir else stat.st_size, extension)
            if not matched:
                continue
            try:
                item = _make_item(runtime, candidate)
            except OSError:
                continue
            results.append((score, item))
        if truncated:
            break
    results.sort(key=lambda pair: (-pair[0], not pair[1].is_dir, pair[1].name.lower()))
    items = [item for _, item in results[:limit]]
    return SearchResponse(query=q, items=items, total=len(results), truncated=truncated)


@router.get("/stat", response_model=FileItem)
def stat_path(
    request: Request,
    path: str,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> FileItem:
    target = _resolve(runtime, request, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    try:
        return _make_item(runtime, target)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/read", response_model=FileReadResponse)
def read_file(
    request: Request,
    path: str,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> FileReadResponse:
    target = _resolve(runtime, request, path)
    if not target.exists() or target.is_dir():
        raise HTTPException(status_code=404, detail="El archivo no existe")
    if target.stat().st_size > MAX_READ_BYTES:
        raise HTTPException(status_code=413, detail="El archivo es demasiado grande para el editor")
    data = target.read_bytes()
    encoding = "utf-8"
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            content = data.decode("latin-1")
            encoding = "latin-1"
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="El archivo no es texto") from exc
    return FileReadResponse(
        path=runtime.relative(target),
        name=target.name,
        content=content,
        size=len(data),
        encoding=encoding,
        editable=is_text_editable(target.name),
    )


@router.put("/write")
def write_file(
    request: Request,
    payload: WriteRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, object]:
    target = _resolve(runtime, request, payload.path)
    if target.is_dir():
        raise HTTPException(status_code=400, detail="No se puede escribir sobre un directorio")
    if not is_text_editable(target.name):
        raise HTTPException(status_code=400, detail="Extensión no editable")
    encoded = payload.content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail="El contenido excede el límite del editor")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        version_id = str(time.time_ns())
        version_dir = runtime.data_dir / "versions" / hashlib.sha256(payload.path.encode("utf-8")).hexdigest()
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, version_dir / f"{version_id}.bak")
        versions = sorted(version_dir.glob("*.bak"), key=lambda item: item.stat().st_mtime, reverse=True)
        for old_version in versions[20:]:
            old_version.unlink(missing_ok=True)
    tmp = target.with_name(target.name + ".vortex-tmp")
    tmp.write_bytes(encoded)
    tmp.replace(target)
    _audit(runtime, request, "FILE_WRITE", runtime.relative(target), 200, {"size": len(encoded)})
    return {"ok": True, "path": runtime.relative(target), "size": len(encoded)}


@router.get("/versions")
def list_versions(
    request: Request,
    path: str,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, object]:
    target = _resolve(runtime, request, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="El archivo no existe")
    version_dir = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    versions = []
    if version_dir.is_dir():
        for item in sorted(version_dir.glob("*.bak"), key=lambda entry: entry.stat().st_mtime, reverse=True):
            versions.append({"id": item.stem, "created": item.stat().st_mtime, "size": item.stat().st_size})
    return {"path": path, "versions": versions}


@router.post("/versions/restore")
def restore_version(
    request: Request,
    path: str,
    version_id: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, object]:
    if not version_id.isdigit():
        raise HTTPException(status_code=400, detail="Versión inválida")
    target = _resolve(runtime, request, path)
    version_dir = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    snapshot = version_dir / f"{version_id}.bak"
    if not snapshot.is_file() or not target.is_file():
        raise HTTPException(status_code=404, detail="La versión ya no está disponible")
    current_id = str(time.time_ns())
    shutil.copy2(target, version_dir / f"{current_id}.bak")
    temporary = target.with_name(target.name + ".vortex-restore")
    shutil.copyfile(snapshot, temporary)
    temporary.replace(target)
    _audit(runtime, request, "FILE_VERSION_RESTORE", path, 200, {"version_id": version_id})
    return {"ok": True, "path": path}


@router.delete("/versions")
def delete_versions(
    request: Request,
    path: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, object]:
    _resolve(runtime, request, path)
    version_dir = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    removed = 0
    if version_dir.is_dir():
        removed = len(list(version_dir.glob("*.bak")))
        shutil.rmtree(version_dir)
    _audit(runtime, request, "FILE_VERSIONS_CLEAR", path, 200, {"removed": removed})
    return {"ok": True, "removed": removed}


@router.delete("/versions/{version_id}")
def delete_version(
    request: Request,
    version_id: str,
    path: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, object]:
    if not version_id.isdigit():
        raise HTTPException(status_code=400, detail="Versión inválida")
    _resolve(runtime, request, path)
    version_dir = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    snapshot = version_dir / f"{version_id}.bak"
    if not snapshot.is_file():
        raise HTTPException(status_code=404, detail="La versión ya no está disponible")
    snapshot.unlink()
    _audit(runtime, request, "FILE_VERSION_DELETE", path, 200, {"version_id": version_id})
    return {"ok": True, "version_id": version_id}


@router.post("/mkdir", response_model=FileItem, status_code=201)
def make_directory(
    request: Request,
    payload: MkdirRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> FileItem:
    parent = _resolve(runtime, request, payload.path)
    if not parent.is_dir():
        raise HTTPException(status_code=400, detail="La ruta padre no es un directorio")
    name = Path(payload.name).name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Nombre inválido")
    destination = parent / name
    if destination.exists():
        raise HTTPException(status_code=409, detail="Ya existe un elemento con ese nombre")
    try:
        destination.mkdir()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _audit(runtime, request, "FILE_MKDIR", runtime.relative(destination), 201)
    return _make_item(runtime, destination)


@router.put("/rename", response_model=FileItem)
def rename_path(
    request: Request,
    payload: RenameRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> FileItem:
    target = _resolve(runtime, request, payload.path)
    if target == runtime.base_dir:
        raise HTTPException(status_code=400, detail="No se puede renombrar la raíz del servidor")
    if not target.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    name = Path(payload.new_name).name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Nombre inválido")
    destination = target.with_name(name)
    if destination.exists() and destination != target:
        raise HTTPException(status_code=409, detail="Ya existe un elemento con ese nombre")
    try:
        target.rename(destination)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _audit(runtime, request, "FILE_RENAME", runtime.relative(destination), 200, {"from": payload.path, "to": name})
    return _make_item(runtime, destination)


@router.delete("")
@router.delete("/")
def delete_path(
    request: Request,
    path: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, bool]:
    target = _resolve(runtime, request, path)
    if target == runtime.base_dir:
        raise HTTPException(status_code=400, detail="No se puede eliminar la raíz del servidor")
    if not target.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    relative = runtime.relative(target)
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _audit(runtime, request, "FILE_DELETE", relative, 200)
    return {"ok": True}


@router.get("/download")
def download_file(
    request: Request,
    path: str,
    inline: bool = False,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> Response:
    target = _resolve(runtime, request, path)
    if not target.exists() or target.is_dir():
        raise HTTPException(status_code=404, detail="El archivo no existe")
    size = target.stat().st_size
    extension = file_extension(target.name)
    if extension in FORCE_ATTACHMENT_EXTS:
        inline = False
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(target.name, inline),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=0",
    }
    _audit(runtime, request, "FILE_DOWNLOAD", runtime.relative(target), 200, {"size": size, "inline": inline})
    if size == 0:
        headers["Content-Length"] = "0"
        return Response(content=b"", media_type=media_type, headers=headers)
    try:
        byte_range = parse_range(request.headers.get("range"), size)
    except (ValueError, TypeError):
        headers["Content-Range"] = f"bytes */{size}"
        return Response(status_code=416, headers=headers)
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(iter_file(target), media_type=media_type, headers=headers)
    start, end = byte_range
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(end - start + 1)
    return StreamingResponse(iter_file(target, start, end), status_code=206, media_type=media_type, headers=headers)


@router.get("/zip")
def download_zip(
    request: Request,
    path: str = "",
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> StreamingResponse:
    target = _resolve(runtime, request, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    zip_name = f"{target.name or 'vortex'}.zip"
    _audit(runtime, request, "FILE_ZIP", runtime.relative(target), 200)
    stream = ZipStream.from_path(str(target))
    headers = {
        "Content-Disposition": _content_disposition(zip_name, False),
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(iter(stream), media_type="application/zip", headers=headers)


@router.post("/upload", response_model=UploadCompleteResponse, status_code=201)
async def upload_direct(
    request: Request,
    file: UploadFile = File(...),
    target_dir: str = Form(""),
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadCompleteResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    destination_dir = _resolve(runtime, request, target_dir)
    if not destination_dir.is_dir():
        raise HTTPException(status_code=400, detail="La carpeta destino no existe")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    try:
        ensure_free_space(destination_dir, declared)
    except InsufficientStorage as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    name = Path(file.filename).name.strip()
    if not name or name in {".", ".."}:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    destination = runtime.unique(destination_dir, name)
    temporary = destination.with_name(destination.name + ".vortex-upload")
    size = 0
    try:
        with open(temporary, "wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                output.write(chunk)
        temporary.replace(destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el archivo: {exc}") from exc
    finally:
        await file.close()
    _audit(runtime, request, "FILE_UPLOAD", runtime.relative(destination), 201, {"size": size, "mode": "direct"})
    return UploadCompleteResponse(path=runtime.relative(destination), name=destination.name, size=size)


@router.post("/upload/init", response_model=UploadInitResponse)
def upload_init(
    request: Request,
    payload: UploadInitRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadInitResponse:
    try:
        meta = runtime.uploads.init(payload.filename, payload.size, payload.target_dir, payload.chunk_size)
    except InsufficientStorage as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except (UploadError, PathTraversalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UploadInitResponse(**meta)


@router.get("/upload/{upload_id}", response_model=UploadStatusResponse)
def upload_status(
    upload_id: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadStatusResponse:
    try:
        meta = runtime.uploads.status(upload_id)
    except UploadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UploadStatusResponse(**meta)


@router.put("/upload/{upload_id}/{index}", response_model=UploadChunkResponse)
async def upload_chunk(
    request: Request,
    upload_id: str,
    index: int,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadChunkResponse:
    data = await request.body()
    try:
        result = runtime.uploads.save_chunk(upload_id, index, data)
    except UploadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return UploadChunkResponse(**result)


@router.post("/upload/{upload_id}/complete", response_model=UploadCompleteResponse, status_code=201)
def upload_complete(
    request: Request,
    upload_id: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadCompleteResponse:
    try:
        destination = runtime.uploads.complete(upload_id)
    except UploadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IncompleteUpload as exc:
        raise HTTPException(status_code=400, detail={"message": "Faltan bloques", "missing": exc.missing}) from exc
    except (UploadError, PathTraversalError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    size = destination.stat().st_size
    _audit(runtime, request, "FILE_UPLOAD", runtime.relative(destination), 201, {"size": size, "mode": "chunked"})
    return UploadCompleteResponse(path=runtime.relative(destination), name=destination.name, size=size)


@router.delete("/upload/{upload_id}")
def upload_abort(
    upload_id: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, bool]:
    try:
        runtime.uploads.abort(upload_id)
    except UploadNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}
