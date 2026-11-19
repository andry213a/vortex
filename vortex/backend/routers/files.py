# Rutas de archivos: listado, búsqueda, lectura, escritura y subidas.

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
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


# Resolución y auditoría de las rutas solicitadas por el cliente.
def _resolve(runtime: Runtime, request: Request, path: str) -> Path:
    try:
        return runtime.resolve(path)
    except PathTraversalError as excepcion:
        runtime.audit.log(
            action="PATH_TRAVERSAL",
            severity=SECURITY,
            client_ip=client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            target_path=path,
            status_code=403,
            details={"error": str(excepcion)},
        )
        raise HTTPException(status_code=403, detail="Ruta no permitida") from excepcion


def _entry_is_safe(runtime: Runtime, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(runtime.base_dir)
    except (OSError, RuntimeError):
        return False


def _make_item(runtime: Runtime, path: Path) -> FileItem:
    estado_archivo = path.stat()
    es_directorio = path.is_dir()
    nombre = path.name
    extension = file_extension(nombre)
    return FileItem(
        name=nombre,
        path=runtime.relative(path),
        is_dir=es_directorio,
        size=0 if es_directorio else estado_archivo.st_size,
        modified=estado_archivo.st_mtime,
        ext=extension,
        kind=file_kind(nombre, es_directorio),
        editable=(not es_directorio and is_text_editable(nombre)),
    )


def _content_disposition(name: str, inline: bool) -> str:
    disposicion = "inline" if inline else "attachment"
    limpio = "".join(caracter for caracter in name if caracter.isprintable() and caracter not in '"\\') or "archivo"
    nombre_ascii = limpio.encode("ascii", "ignore").decode("ascii") or "archivo"
    return f"{disposicion}; filename=\"{nombre_ascii}\"; filename*=UTF-8''{quote(limpio)}"


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
# Listado y búsqueda de archivos dentro de la carpeta compartida.
def list_directory(
    request: Request,
    path: str = "",
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> ListResponse:
    objetivo = _resolve(runtime, request, path)
    if not objetivo.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    if not objetivo.is_dir():
        raise HTTPException(status_code=400, detail="La ruta no es un directorio")
    elementos: list[FileItem] = []
    try:
        with os.scandir(objetivo) as entradas:
            for entrada in entradas:
                candidato = Path(entrada.path)
                if not _entry_is_safe(runtime, candidato):
                    continue
                try:
                    elementos.append(_make_item(runtime, candidato))
                except OSError:
                    continue
    except OSError as excepcion:
        raise HTTPException(status_code=500, detail=f"No se pudo leer el directorio: {excepcion}") from excepcion
    elementos.sort(key=lambda elemento: (not elemento.is_dir, elemento.name.lower()))
    padre = None if objetivo == runtime.base_dir else runtime.relative(objetivo.parent)
    return ListResponse(path=runtime.relative(objetivo), parent=padre, items=elementos, total=len(elementos))


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
    raiz = _resolve(runtime, request, path)
    if not raiz.exists() or not raiz.is_dir():
        raise HTTPException(status_code=404, detail="La carpeta de búsqueda no existe")
    consulta = SearchQuery(q, kind, ext, min_size, max_size)
    resultados: list[tuple[float, FileItem]] = []
    escaneados = 0
    truncado = False
    limite = time.monotonic() + 6.0
    for ruta_directorio, nombres_directorios, nombres_archivos in os.walk(raiz, followlinks=False):
        for nombre in [*nombres_directorios, *nombres_archivos]:
            escaneados += 1
            if escaneados > 25000 or time.monotonic() > limite:
                truncado = True
                break
            candidato = Path(ruta_directorio) / nombre
            if not _entry_is_safe(runtime, candidato):
                continue
            try:
                estado_archivo = candidato.stat()
            except OSError:
                continue
            es_directorio = candidato.is_dir()
            extension = file_extension(nombre)
            coincide, puntuacion = consulta.matches(nombre, es_directorio, 0 if es_directorio else estado_archivo.st_size, extension)
            if not coincide:
                continue
            try:
                elemento = _make_item(runtime, candidato)
            except OSError:
                continue
            resultados.append((puntuacion, elemento))
        if truncado:
            break
    resultados.sort(key=lambda pair: (-pair[0], not pair[1].is_dir, pair[1].name.lower()))
    elementos = [elemento for _, elemento in resultados[:limit]]
    return SearchResponse(query=q, items=elementos, total=len(resultados), truncated=truncado)


@router.get("/stat", response_model=FileItem)
# Metadatos, lectura y escritura de archivos de texto.
def stat_path(
    request: Request,
    path: str,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> FileItem:
    objetivo = _resolve(runtime, request, path)
    if not objetivo.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    try:
        return _make_item(runtime, objetivo)
    except OSError as excepcion:
        raise HTTPException(status_code=400, detail=str(excepcion)) from excepcion


@router.get("/read", response_model=FileReadResponse)
def read_file(
    request: Request,
    path: str,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> FileReadResponse:
    objetivo = _resolve(runtime, request, path)
    if not objetivo.exists() or objetivo.is_dir():
        raise HTTPException(status_code=404, detail="El archivo no existe")
    if objetivo.stat().st_size > MAX_READ_BYTES:
        raise HTTPException(status_code=413, detail="El archivo es demasiado grande para el editor")
    datos = objetivo.read_bytes()
    codificacion = "utf-8"
    try:
        contenido = datos.decode("utf-8")
    except UnicodeDecodeError:
        try:
            contenido = datos.decode("latin-1")
            codificacion = "latin-1"
        except UnicodeDecodeError as excepcion:
            raise HTTPException(status_code=415, detail="El archivo no es texto") from excepcion
    return FileReadResponse(
        path=runtime.relative(objetivo),
        name=objetivo.name,
        content=contenido,
        size=len(datos),
        encoding=codificacion,
        editable=is_text_editable(objetivo.name),
    )


@router.put("/write")
def write_file(
    request: Request,
    payload: WriteRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, object]:
    objetivo = _resolve(runtime, request, payload.path)
    if objetivo.is_dir():
        raise HTTPException(status_code=400, detail="No se puede escribir sobre un directorio")
    if not is_text_editable(objetivo.name):
        raise HTTPException(status_code=400, detail="Extensión no editable")
    codificado = payload.content.encode("utf-8")
    if len(codificado) > MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail="El contenido excede el límite del editor")
    objetivo.parent.mkdir(parents=True, exist_ok=True)
    if objetivo.is_file():
        id_version = str(time.time_ns())
        directorio_versiones = runtime.data_dir / "versions" / hashlib.sha256(payload.path.encode("utf-8")).hexdigest()
        directorio_versiones.mkdir(parents=True, exist_ok=True)
        shutil.copy2(objetivo, directorio_versiones / f"{id_version}.bak")
        versiones = sorted(directorio_versiones.glob("*.bak"), key=lambda elemento: elemento.stat().st_mtime, reverse=True)
        for version_anterior in versiones[20:]:
            version_anterior.unlink(missing_ok=True)
    temporal = objetivo.with_name(objetivo.name + ".vortex-tmp")
    temporal.write_bytes(codificado)
    temporal.replace(objetivo)
    _audit(runtime, request, "FILE_WRITE", runtime.relative(objetivo), 200, {"size": len(codificado)})
    return {"ok": True, "path": runtime.relative(objetivo), "size": len(codificado)}


@router.get("/versions")
def list_versions(
    request: Request,
    path: str,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, object]:
    objetivo = _resolve(runtime, request, path)
    if not objetivo.is_file():
        raise HTTPException(status_code=404, detail="El archivo no existe")
    directorio_versiones = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    versiones = []
    if directorio_versiones.is_dir():
        for elemento in sorted(directorio_versiones.glob("*.bak"), key=lambda entrada: entrada.stat().st_mtime, reverse=True):
            versiones.append({"id": elemento.stem, "created": elemento.stat().st_mtime, "size": elemento.stat().st_size})
    return {"path": path, "versions": versiones}


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
    objetivo = _resolve(runtime, request, path)
    directorio_versiones = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    instantanea = directorio_versiones / f"{version_id}.bak"
    if not instantanea.is_file() or not objetivo.is_file():
        raise HTTPException(status_code=404, detail="La versión ya no está disponible")
    id_actual = str(time.time_ns())
    shutil.copy2(objetivo, directorio_versiones / f"{id_actual}.bak")
    temporal = objetivo.with_name(objetivo.name + ".vortex-restore")
    shutil.copyfile(instantanea, temporal)
    temporal.replace(objetivo)
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
    directorio_versiones = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    eliminados = 0
    if directorio_versiones.is_dir():
        eliminados = len(list(directorio_versiones.glob("*.bak")))
        shutil.rmtree(directorio_versiones)
    _audit(runtime, request, "FILE_VERSIONS_CLEAR", path, 200, {"removed": eliminados})
    return {"ok": True, "removed": eliminados}


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
    directorio_versiones = runtime.data_dir / "versions" / hashlib.sha256(path.encode("utf-8")).hexdigest()
    instantanea = directorio_versiones / f"{version_id}.bak"
    if not instantanea.is_file():
        raise HTTPException(status_code=404, detail="La versión ya no está disponible")
    instantanea.unlink()
    _audit(runtime, request, "FILE_VERSION_DELETE", path, 200, {"version_id": version_id})
    return {"ok": True, "version_id": version_id}


@router.post("/mkdir", response_model=FileItem, status_code=201)
def make_directory(
    request: Request,
    payload: MkdirRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> FileItem:
    padre = _resolve(runtime, request, payload.path)
    if not padre.is_dir():
        raise HTTPException(status_code=400, detail="La ruta padre no es un directorio")
    nombre = Path(payload.name).name.strip()
    if not nombre or nombre in {".", ".."} or "/" in nombre or "\\" in nombre:
        raise HTTPException(status_code=400, detail="Nombre inválido")
    destino = padre / nombre
    if destino.exists():
        raise HTTPException(status_code=409, detail="Ya existe un elemento con ese nombre")
    try:
        destino.mkdir()
    except OSError as excepcion:
        raise HTTPException(status_code=500, detail=str(excepcion)) from excepcion
    _audit(runtime, request, "FILE_MKDIR", runtime.relative(destino), 201)
    return _make_item(runtime, destino)


@router.put("/rename", response_model=FileItem)
def rename_path(
    request: Request,
    payload: RenameRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> FileItem:
    objetivo = _resolve(runtime, request, payload.path)
    if objetivo == runtime.base_dir:
        raise HTTPException(status_code=400, detail="No se puede renombrar la raíz del servidor")
    if not objetivo.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    nombre = Path(payload.new_name).name.strip()
    if not nombre or nombre in {".", ".."} or "/" in nombre or "\\" in nombre:
        raise HTTPException(status_code=400, detail="Nombre inválido")
    destino = objetivo.with_name(nombre)
    if destino.exists() and destino != objetivo:
        raise HTTPException(status_code=409, detail="Ya existe un elemento con ese nombre")
    try:
        objetivo.rename(destino)
    except OSError as excepcion:
        raise HTTPException(status_code=500, detail=str(excepcion)) from excepcion
    _audit(
        runtime,
        request,
        "FILE_RENAME",
        runtime.relative(destino),
        200,
        {"from": payload.path, "to": nombre, "is_dir": destino.is_dir()},
    )
    return _make_item(runtime, destino)


@router.delete("")
@router.delete("/")
def delete_path(
    request: Request,
    path: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, bool]:
    objetivo = _resolve(runtime, request, path)
    if objetivo == runtime.base_dir:
        raise HTTPException(status_code=400, detail="No se puede eliminar la raíz del servidor")
    if not objetivo.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    relativa = runtime.relative(objetivo)
    es_directorio = objetivo.is_dir()
    try:
        if objetivo.is_dir():
            shutil.rmtree(objetivo)
        else:
            objetivo.unlink()
    except OSError as excepcion:
        raise HTTPException(status_code=500, detail=str(excepcion)) from excepcion
    _audit(runtime, request, "FILE_DELETE", relativa, 200, {"is_dir": es_directorio})
    return {"ok": True}


@router.get("/download")
# Descargas con soporte de rangos y protección de contenido activo.
def download_file(
    request: Request,
    path: str,
    inline: bool = False,
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> Response:
    objetivo = _resolve(runtime, request, path)
    if not objetivo.exists() or objetivo.is_dir():
        raise HTTPException(status_code=404, detail="El archivo no existe")
    tamano = objetivo.stat().st_size
    extension = file_extension(objetivo.name)
    if extension in FORCE_ATTACHMENT_EXTS:
        inline = False
    tipo_medio = mimetypes.guess_type(objetivo.name)[0] or "application/octet-stream"
    cabeceras = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(objetivo.name, inline),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=0",
    }
    _audit(runtime, request, "FILE_DOWNLOAD", runtime.relative(objetivo), 200, {"size": tamano, "inline": inline})
    if tamano == 0:
        cabeceras["Content-Length"] = "0"
        return Response(content=b"", media_type=tipo_medio, headers=cabeceras)
    try:
        rango_bytes = parse_range(request.headers.get("range"), tamano)
    except (ValueError, TypeError):
        cabeceras["Content-Range"] = f"bytes */{tamano}"
        return Response(status_code=416, headers=cabeceras)
    if rango_bytes is None:
        cabeceras["Content-Length"] = str(tamano)
        return StreamingResponse(iter_file(objetivo), media_type=tipo_medio, headers=cabeceras)
    inicio, fin = rango_bytes
    cabeceras["Content-Range"] = f"bytes {inicio}-{fin}/{tamano}"
    cabeceras["Content-Length"] = str(fin - inicio + 1)
    return StreamingResponse(iter_file(objetivo, inicio, fin), status_code=206, media_type=tipo_medio, headers=cabeceras)


@router.get("/zip")
def download_zip(
    request: Request,
    path: str = "",
    access: Access = Depends(require_read),
    runtime: Runtime = Depends(get_runtime),
) -> StreamingResponse:
    objetivo = _resolve(runtime, request, path)
    if not objetivo.exists():
        raise HTTPException(status_code=404, detail="La ruta no existe")
    nombre_zip = f"{objetivo.name or 'vortex'}.zip"
    _audit(runtime, request, "FILE_ZIP", runtime.relative(objetivo), 200)
    flujo = ZipStream.from_path(str(objetivo))
    cabeceras = {
        "Content-Disposition": _content_disposition(nombre_zip, False),
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(iter(flujo), media_type="application/zip", headers=cabeceras)


# Subidas directas y por bloques con limpieza de restos.
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
    directorio_destino = _resolve(runtime, request, target_dir)
    if not directorio_destino.is_dir():
        raise HTTPException(status_code=400, detail="La carpeta destino no existe")
    try:
        declarado = int(request.headers.get("content-length") or 0)
    except ValueError:
        declarado = 0
    try:
        ensure_free_space(directorio_destino, declarado)
    except InsufficientStorage as excepcion:
        raise HTTPException(status_code=507, detail=str(excepcion)) from excepcion
    nombre = Path(file.filename).name.strip()
    if not nombre or nombre in {".", ".."}:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    destino = runtime.unique(directorio_destino, nombre)
    temporal = destino.with_name(destino.name + ".vortex-upload")
    tamano = 0
    try:
        salida = await run_in_threadpool(open, temporal, "wb")
        try:
            while True:
                bloque = await file.read(1024 * 1024)
                if not bloque:
                    break
                tamano += len(bloque)
                await run_in_threadpool(salida.write, bloque)
        finally:
            await run_in_threadpool(salida.close)
        temporal.replace(destino)
    except OSError as excepcion:
        temporal.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el archivo: {excepcion}") from excepcion
    finally:
        await file.close()
    _audit(runtime, request, "FILE_UPLOAD", runtime.relative(destino), 201, {"size": tamano, "mode": "direct"})
    return UploadCompleteResponse(path=runtime.relative(destino), name=destino.name, size=tamano)


@router.post("/upload/init", response_model=UploadInitResponse)
def upload_init(
    request: Request,
    payload: UploadInitRequest,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadInitResponse:
    try:
        metadatos = runtime.uploads.init(payload.filename, payload.size, payload.target_dir, payload.chunk_size)
    except InsufficientStorage as excepcion:
        raise HTTPException(status_code=507, detail=str(excepcion)) from excepcion
    except (UploadError, PathTraversalError) as excepcion:
        raise HTTPException(status_code=400, detail=str(excepcion)) from excepcion
    return UploadInitResponse(**metadatos)


@router.get("/upload/{upload_id}", response_model=UploadStatusResponse)
def upload_status(
    upload_id: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadStatusResponse:
    try:
        metadatos = runtime.uploads.status(upload_id)
    except UploadNotFound as excepcion:
        raise HTTPException(status_code=404, detail=str(excepcion)) from excepcion
    except UploadError as excepcion:
        raise HTTPException(status_code=400, detail=str(excepcion)) from excepcion
    return UploadStatusResponse(**metadatos)


@router.put("/upload/{upload_id}/{index}", response_model=UploadChunkResponse)
async def upload_chunk(
    request: Request,
    upload_id: str,
    index: int,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadChunkResponse:
    datos = await request.body()
    try:
        resultado = runtime.uploads.save_chunk(upload_id, index, datos)
    except UploadNotFound as excepcion:
        raise HTTPException(status_code=404, detail=str(excepcion)) from excepcion
    except UploadError as excepcion:
        raise HTTPException(status_code=400, detail=str(excepcion)) from excepcion
    return UploadChunkResponse(**resultado)


@router.post("/upload/{upload_id}/complete", response_model=UploadCompleteResponse, status_code=201)
def upload_complete(
    request: Request,
    upload_id: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> UploadCompleteResponse:
    try:
        destino = runtime.uploads.complete(upload_id)
    except UploadNotFound as excepcion:
        raise HTTPException(status_code=404, detail=str(excepcion)) from excepcion
    except IncompleteUpload as excepcion:
        raise HTTPException(status_code=400, detail={"message": "Faltan bloques", "missing": excepcion.missing}) from excepcion
    except (UploadError, PathTraversalError) as excepcion:
        raise HTTPException(status_code=400, detail=str(excepcion)) from excepcion
    tamano = destino.stat().st_size
    _audit(runtime, request, "FILE_UPLOAD", runtime.relative(destino), 201, {"size": tamano, "mode": "chunked"})
    return UploadCompleteResponse(path=runtime.relative(destino), name=destino.name, size=tamano)


@router.delete("/upload/{upload_id}")
def upload_abort(
    upload_id: str,
    access: Access = Depends(require_write),
    runtime: Runtime = Depends(get_runtime),
) -> dict[str, bool]:
    try:
        runtime.uploads.abort(upload_id)
    except UploadNotFound as excepcion:
        raise HTTPException(status_code=404, detail=str(excepcion)) from excepcion
    return {"ok": True}
