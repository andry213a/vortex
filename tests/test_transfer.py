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
    payload = {
        "filename": "archivo.bin",
        "size": 3 * CHUNK,
        "target_dir": "",
        "chunk_size": CHUNK,
    }
    payload.update(overrides)
    return client.post("/api/files/upload/init", json=payload, headers=headers)


def test_init_returns_metadata_without_chunks(app_bundle, auth_headers):
    response = _init_upload(app_bundle, auth_headers, filename="inicial.bin")
    assert response.status_code == 200, response.text
    info = response.json()
    assert info["filename"] == "inicial.bin"
    assert info["chunk_size"] == CHUNK
    assert info["chunk_count"] == 3
    assert info["received"] == []
    assert len(info["upload_id"]) == 32

    status = app_bundle.get(f"/api/files/upload/{info['upload_id']}", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["received"] == []


def test_chunks_can_arrive_out_of_order_and_resume(app_bundle, auth_headers):
    payload = b"A" * CHUNK + b"B" * CHUNK + b"C" * 123
    info = _init_upload(app_bundle, auth_headers, size=len(payload)).json()
    upload_id = info["upload_id"]

    last = app_bundle.put(
        f"/api/files/upload/{upload_id}/2",
        content=payload[2 * CHUNK :],
        headers=auth_headers,
    )
    assert last.status_code == 200
    assert last.json()["received"] == [2]

    resumed = app_bundle.get(f"/api/files/upload/{upload_id}", headers=auth_headers)
    assert resumed.json()["received"] == [2]

    for index in (0, 1):
        chunk = payload[index * CHUNK : (index + 1) * CHUNK]
        response = app_bundle.put(
            f"/api/files/upload/{upload_id}/{index}",
            content=chunk,
            headers=auth_headers,
        )
        assert response.status_code == 200
    assert response.json()["received_count"] == 3

    complete = app_bundle.post(f"/api/files/upload/{upload_id}/complete", headers=auth_headers)
    assert complete.status_code == 201, complete.text
    assert (app_bundle.base_dir / "archivo.bin").read_bytes() == payload
    assert app_bundle.get(f"/api/files/upload/{upload_id}", headers=auth_headers).status_code == 404


def test_complete_reports_missing_chunks(app_bundle, auth_headers):
    info = _init_upload(app_bundle, auth_headers, filename="parcial.bin").json()
    upload_id = info["upload_id"]
    app_bundle.put(
        f"/api/files/upload/{upload_id}/0",
        content=b"x" * CHUNK,
        headers=auth_headers,
    )
    complete = app_bundle.post(f"/api/files/upload/{upload_id}/complete", headers=auth_headers)
    assert complete.status_code == 400
    assert complete.json()["detail"]["missing"] == [1, 2]
    assert not (app_bundle.base_dir / "parcial.bin").exists()


def test_complete_renames_on_conflict(app_bundle, auth_headers):
    info = _init_upload(app_bundle, auth_headers, filename="hola.txt", size=len(b"nuevo")).json()
    upload_id = info["upload_id"]
    app_bundle.put(
        f"/api/files/upload/{upload_id}/0",
        content=b"nuevo",
        headers=auth_headers,
    )
    complete = app_bundle.post(f"/api/files/upload/{upload_id}/complete", headers=auth_headers)
    assert complete.status_code == 201, complete.text
    assert complete.json()["name"] == "hola_1.txt"
    assert (app_bundle.base_dir / "hola_1.txt").read_bytes() == b"nuevo"


def test_abort_removes_upload_and_is_idempotent(app_bundle, auth_headers):
    info = _init_upload(app_bundle, auth_headers, filename="cancelar.bin").json()
    upload_id = info["upload_id"]
    upload_dir = app_bundle.runtime.uploads.root / upload_id
    assert upload_dir.is_dir()

    aborted = app_bundle.delete(f"/api/files/upload/{upload_id}", headers=auth_headers)
    assert aborted.status_code == 200
    assert not upload_dir.exists()
    assert app_bundle.get(f"/api/files/upload/{upload_id}", headers=auth_headers).status_code == 404

    again = app_bundle.delete(f"/api/files/upload/{upload_id}", headers=auth_headers)
    assert again.status_code == 200


def test_unknown_and_malformed_upload_ids(app_bundle, auth_headers):
    missing = f"/api/files/upload/{uuid.uuid4().hex}"
    assert app_bundle.get(missing, headers=auth_headers).status_code == 404
    malformed = app_bundle.get("/api/files/upload/no-es-un-id", headers=auth_headers)
    assert malformed.status_code == 404
    assert app_bundle.put(
        f"{missing}/0",
        content=b"x",
        headers=auth_headers,
    ).status_code == 404
    assert app_bundle.post(f"{missing}/complete", headers=auth_headers).status_code == 404


def test_chunk_index_and_size_validation(app_bundle, auth_headers):
    info = _init_upload(app_bundle, auth_headers, filename="limites.bin").json()
    upload_id = info["upload_id"]
    out_of_range = app_bundle.put(
        f"/api/files/upload/{upload_id}/99",
        content=b"x",
        headers=auth_headers,
    )
    assert out_of_range.status_code == 400
    too_big = app_bundle.put(
        f"/api/files/upload/{upload_id}/0",
        content=b"x" * (CHUNK + 1),
        headers=auth_headers,
    )
    assert too_big.status_code == 400
    negative = app_bundle.put(
        f"/api/files/upload/{upload_id}/-1",
        content=b"x",
        headers=auth_headers,
    )
    assert negative.status_code == 400


def test_init_rejects_bad_targets_and_names(app_bundle, auth_headers):
    assert _init_upload(app_bundle, auth_headers, target_dir="../fuera").status_code == 400
    assert _init_upload(app_bundle, auth_headers, target_dir="no-existe").status_code == 400
    assert _init_upload(app_bundle, auth_headers, filename="..").status_code == 400
    assert _init_upload(app_bundle, auth_headers, size=-1).status_code == 422


def test_empty_file_roundtrip(app_bundle, auth_headers):
    info = _init_upload(app_bundle, auth_headers, filename="vacio.bin", size=0).json()
    assert info["chunk_count"] == 1
    upload_id = info["upload_id"]
    saved = app_bundle.put(
        f"/api/files/upload/{upload_id}/0",
        content=b"",
        headers=auth_headers,
    )
    assert saved.status_code == 200
    complete = app_bundle.post(f"/api/files/upload/{upload_id}/complete", headers=auth_headers)
    assert complete.status_code == 201, complete.text
    assert (app_bundle.base_dir / "vacio.bin").read_bytes() == b""


def test_cleanup_stale_keeps_active_uploads(tmp_path, base_dir):
    manager = UploadManager(tmp_path / "uploads", base_dir, CHUNK)
    active_id = manager.init("activo.bin", CHUNK, "")["upload_id"]
    stale = manager.init("viejo.bin", CHUNK, "")
    stale_dir = manager.root / stale["upload_id"]
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(stale_dir, (old, old))

    stale_orphan = manager.root / ("a" * 32)
    stale_orphan.mkdir()
    os.utime(stale_orphan, (old, old))
    foreign = manager.root / "no-es-un-id"
    foreign.mkdir()
    os.utime(foreign, (old, old))
    old_file = manager.root / ("b" * 32)
    old_file.write_bytes(b"x")
    os.utime(old_file, (old, old))

    removed = manager.cleanup_stale(max_age_seconds=60 * 60)
    assert removed == 2
    assert (manager.root / active_id).is_dir()
    assert not stale_dir.exists()
    assert not stale_orphan.exists()
    assert foreign.is_dir()
    assert old_file.is_file()


def test_manager_rejects_corrupt_metadata(tmp_path, base_dir):
    manager = UploadManager(tmp_path / "uploads", base_dir, CHUNK)
    upload_id = manager.init("meta.bin", CHUNK, "")["upload_id"]
    (manager.root / upload_id / "meta.json").write_text("{no-json", encoding="utf-8")
    with pytest.raises(UploadError):
        manager.status(upload_id)
    with pytest.raises(UploadNotFound):
        manager.status("f" * 32)


def test_ensure_free_space_and_unique_path(tmp_path):
    with pytest.raises(InsufficientStorage):
        ensure_free_space(tmp_path, 10, margin=10**18)
    ensure_free_space(tmp_path, 0)

    (tmp_path / "nota.md").write_text("x", encoding="utf-8")
    assert unique_path(tmp_path, "nota.md").name == "nota_1.md"
    with pytest.raises(UploadError):
        unique_path(tmp_path, "..")


def test_parse_range_and_iter_file(tmp_path):
    path = tmp_path / "datos.bin"
    path.write_bytes(b"0123456789")
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
    assert b"".join(iter_file(path, 2, 5, block_size=2)) == b"2345"


def test_upload_ids_are_validated(tmp_path, base_dir):
    manager = UploadManager(tmp_path / "uploads", base_dir, CHUNK)
    with pytest.raises(UploadNotFound):
        manager.status("../../etc/passwd")


def test_upload_dir_metadata_is_persisted(app_bundle, auth_headers):
    info = _init_upload(app_bundle, auth_headers, filename="persistente.bin").json()
    upload_id = info["upload_id"]
    meta_path = Path(app_bundle.runtime.uploads.root / upload_id / "meta.json")
    assert meta_path.is_file()
    assert meta_path.read_text(encoding="utf-8")
