# Pruebas de subidas por bloques, reanudación, aborto y limpieza.

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

from vortex.backend.transfer import (
    InsufficientStorage,
    UploadError,
    UploadManager,
    UploadNotFound,
    ensure_free_space,
    iter_file,
    parse_range,
    unique_path,
)

CHUNK = 256 * 1024


def _init_upload(client, headers, **overrides):
    carga = {
        "filename": "archivo.bin",
        "size": 3 * CHUNK,
        "target_dir": "",
        "chunk_size": CHUNK,
    }
    carga.update(overrides)
    return client.post("/api/files/upload/init", json=carga, headers=headers)


def test_init_returns_metadata_without_chunks(app_bundle, auth_headers):
    respuesta = _init_upload(app_bundle, auth_headers, filename="inicial.bin")
    assert respuesta.status_code == 200, respuesta.text
    informacion = respuesta.json()
    assert informacion["filename"] == "inicial.bin"
    assert informacion["chunk_size"] == CHUNK
    assert informacion["chunk_count"] == 3
    assert informacion["received"] == []
    assert len(informacion["upload_id"]) == 32

    estado = app_bundle.get(f"/api/files/upload/{informacion['upload_id']}", headers=auth_headers)
    assert estado.status_code == 200
    assert estado.json()["received"] == []


def test_chunks_can_arrive_out_of_order_and_resume(app_bundle, auth_headers):
    carga = b"A" * CHUNK + b"B" * CHUNK + b"C" * 123
    informacion = _init_upload(app_bundle, auth_headers, size=len(carga)).json()
    id_subida = informacion["upload_id"]

    ultimo = app_bundle.put(
        f"/api/files/upload/{id_subida}/2",
        content=carga[2 * CHUNK :],
        headers=auth_headers,
    )
    assert ultimo.status_code == 200
    assert ultimo.json()["received"] == [2]

    reanudado = app_bundle.get(f"/api/files/upload/{id_subida}", headers=auth_headers)
    assert reanudado.json()["received"] == [2]

    for indice in (0, 1):
        bloque = carga[indice * CHUNK : (indice + 1) * CHUNK]
        respuesta = app_bundle.put(
            f"/api/files/upload/{id_subida}/{indice}",
            content=bloque,
            headers=auth_headers,
        )
        assert respuesta.status_code == 200
    assert respuesta.json()["received_count"] == 3

    completar = app_bundle.post(f"/api/files/upload/{id_subida}/complete", headers=auth_headers)
    assert completar.status_code == 201, completar.text
    assert (app_bundle.base_dir / "archivo.bin").read_bytes() == carga
    assert app_bundle.get(f"/api/files/upload/{id_subida}", headers=auth_headers).status_code == 404


def test_complete_reports_missing_chunks(app_bundle, auth_headers):
    informacion = _init_upload(app_bundle, auth_headers, filename="parcial.bin").json()
    id_subida = informacion["upload_id"]
    app_bundle.put(
        f"/api/files/upload/{id_subida}/0",
        content=b"x" * CHUNK,
        headers=auth_headers,
    )
    completar = app_bundle.post(f"/api/files/upload/{id_subida}/complete", headers=auth_headers)
    assert completar.status_code == 400
    assert completar.json()["detail"]["missing"] == [1, 2]
    assert not (app_bundle.base_dir / "parcial.bin").exists()


def test_complete_renames_on_conflict(app_bundle, auth_headers):
    informacion = _init_upload(app_bundle, auth_headers, filename="hola.txt", size=len(b"nuevo")).json()
    id_subida = informacion["upload_id"]
    app_bundle.put(
        f"/api/files/upload/{id_subida}/0",
        content=b"nuevo",
        headers=auth_headers,
    )
    completar = app_bundle.post(f"/api/files/upload/{id_subida}/complete", headers=auth_headers)
    assert completar.status_code == 201, completar.text
    assert completar.json()["name"] == "hola_1.txt"
    assert (app_bundle.base_dir / "hola_1.txt").read_bytes() == b"nuevo"


def test_abort_removes_upload_and_is_idempotent(app_bundle, auth_headers):
    informacion = _init_upload(app_bundle, auth_headers, filename="cancelar.bin").json()
    id_subida = informacion["upload_id"]
    directorio_subida = app_bundle.runtime.uploads.root / id_subida
    assert directorio_subida.is_dir()

    abortado = app_bundle.delete(f"/api/files/upload/{id_subida}", headers=auth_headers)
    assert abortado.status_code == 200
    assert not directorio_subida.exists()
    assert app_bundle.get(f"/api/files/upload/{id_subida}", headers=auth_headers).status_code == 404

    otra_vez = app_bundle.delete(f"/api/files/upload/{id_subida}", headers=auth_headers)
    assert otra_vez.status_code == 200


def test_unknown_and_malformed_upload_ids(app_bundle, auth_headers):
    faltantes = f"/api/files/upload/{uuid.uuid4().hex}"
    assert app_bundle.get(faltantes, headers=auth_headers).status_code == 404
    malformado = app_bundle.get("/api/files/upload/no-es-un-id", headers=auth_headers)
    assert malformado.status_code == 404
    assert app_bundle.put(
        f"{faltantes}/0",
        content=b"x",
        headers=auth_headers,
    ).status_code == 404
    assert app_bundle.post(f"{faltantes}/complete", headers=auth_headers).status_code == 404


def test_chunk_index_and_size_validation(app_bundle, auth_headers):
    informacion = _init_upload(app_bundle, auth_headers, filename="limites.bin").json()
    id_subida = informacion["upload_id"]
    fuera_rango = app_bundle.put(
        f"/api/files/upload/{id_subida}/99",
        content=b"x",
        headers=auth_headers,
    )
    assert fuera_rango.status_code == 400
    demasiado_grande = app_bundle.put(
        f"/api/files/upload/{id_subida}/0",
        content=b"x" * (CHUNK + 1),
        headers=auth_headers,
    )
    assert demasiado_grande.status_code == 400
    negativo = app_bundle.put(
        f"/api/files/upload/{id_subida}/-1",
        content=b"x",
        headers=auth_headers,
    )
    assert negativo.status_code == 400


def test_init_rejects_bad_targets_and_names(app_bundle, auth_headers):
    assert _init_upload(app_bundle, auth_headers, target_dir="../fuera").status_code == 400
    assert _init_upload(app_bundle, auth_headers, target_dir="no-existe").status_code == 400
    assert _init_upload(app_bundle, auth_headers, filename="..").status_code == 400
    assert _init_upload(app_bundle, auth_headers, size=-1).status_code == 422


def test_empty_file_roundtrip(app_bundle, auth_headers):
    informacion = _init_upload(app_bundle, auth_headers, filename="vacio.bin", size=0).json()
    assert informacion["chunk_count"] == 1
    id_subida = informacion["upload_id"]
    guardado = app_bundle.put(
        f"/api/files/upload/{id_subida}/0",
        content=b"",
        headers=auth_headers,
    )
    assert guardado.status_code == 200
    completar = app_bundle.post(f"/api/files/upload/{id_subida}/complete", headers=auth_headers)
    assert completar.status_code == 201, completar.text
    assert (app_bundle.base_dir / "vacio.bin").read_bytes() == b""


def test_cleanup_stale_keeps_active_uploads(tmp_path, base_dir):
    gestor = UploadManager(tmp_path / "uploads", base_dir, CHUNK)
    id_activo = gestor.init("activo.bin", CHUNK, "")["upload_id"]
    obsoleto = gestor.init("viejo.bin", CHUNK, "")
    directorio_obsoleto = gestor.root / obsoleto["upload_id"]
    anterior = time.time() - 2 * 24 * 60 * 60
    os.utime(directorio_obsoleto, (anterior, anterior))

    huerfano_obsoleto = gestor.root / ("a" * 32)
    huerfano_obsoleto.mkdir()
    os.utime(huerfano_obsoleto, (anterior, anterior))
    ajeno = gestor.root / "no-es-un-id"
    ajeno.mkdir()
    os.utime(ajeno, (anterior, anterior))
    archivo_anterior = gestor.root / ("b" * 32)
    archivo_anterior.write_bytes(b"x")
    os.utime(archivo_anterior, (anterior, anterior))

    eliminados = gestor.cleanup_stale(max_age_seconds=60 * 60)
    assert eliminados == 2
    assert (gestor.root / id_activo).is_dir()
    assert not directorio_obsoleto.exists()
    assert not huerfano_obsoleto.exists()
    assert ajeno.is_dir()
    assert archivo_anterior.is_file()


def test_manager_rejects_corrupt_metadata(tmp_path, base_dir):
    gestor = UploadManager(tmp_path / "uploads", base_dir, CHUNK)
    id_subida = gestor.init("meta.bin", CHUNK, "")["upload_id"]
    (gestor.root / id_subida / "meta.json").write_text("{no-json", encoding="utf-8")
    with pytest.raises(UploadError):
        gestor.status(id_subida)
    with pytest.raises(UploadNotFound):
        gestor.status("f" * 32)


def test_ensure_free_space_and_unique_path(tmp_path):
    with pytest.raises(InsufficientStorage):
        ensure_free_space(tmp_path, 10, margin=10**18)
    ensure_free_space(tmp_path, 0)

    (tmp_path / "nota.md").write_text("x", encoding="utf-8")
    assert unique_path(tmp_path, "nota.md").name == "nota_1.md"
    with pytest.raises(UploadError):
        unique_path(tmp_path, "..")


def test_parse_range_and_iter_file(tmp_path):
    ruta = tmp_path / "datos.bin"
    ruta.write_bytes(b"0123456789")
    assert parse_range(None, 10) is None
    assert parse_range("items=0-1", 10) is None
    assert parse_range("bytes=2-5", 10) == (2, 5)
    assert parse_range("bytes=5-", 10) == (5, 9)
    assert parse_range("bytes=-3", 10) == (7, 9)
    assert parse_range("bytes=0-99", 10) == (0, 9)
    with pytest.raises(ValueError):
        parse_range("bytes=99-", 10)
    with pytest.raises(ValueError):
        parse_range("bytes=-0", 10)
    assert b"".join(iter_file(ruta, 2, 5, block_size=2)) == b"2345"


def test_upload_ids_are_validated(tmp_path, base_dir):
    gestor = UploadManager(tmp_path / "uploads", base_dir, CHUNK)
    with pytest.raises(UploadNotFound):
        gestor.status("../../etc/passwd")


def test_upload_dir_metadata_is_persisted(app_bundle, auth_headers):
    informacion = _init_upload(app_bundle, auth_headers, filename="persistente.bin").json()
    id_subida = informacion["upload_id"]
    ruta_meta = Path(app_bundle.runtime.uploads.root / id_subida / "meta.json")
    assert ruta_meta.is_file()
    assert ruta_meta.read_text(encoding="utf-8")
