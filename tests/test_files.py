from __future__ import annotations


def test_list_and_parent(app_bundle, auth_headers):
    response = app_bundle.get("/api/files", params={"path": ""}, headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    names = {item["name"] for item in data["items"]}
    assert {"hola.txt", "docs"} <= names
    assert data["parent"] is None
    docs = app_bundle.get("/api/files", params={"path": "docs"}, headers=auth_headers)
    assert docs.status_code == 200
    assert docs.json()["parent"] == ""


def test_fuzzy_search_and_filters(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/search", params={"q": "nota"}, headers=auth_headers)
    assert response.status_code == 200
    assert any(item["name"] == "nota.md" for item in response.json()["items"])
    filtered = app_bundle.get("/api/files/search", params={"q": "ext:md"}, headers=auth_headers)
    assert filtered.status_code == 200
    assert [item["name"] for item in filtered.json()["items"]] == ["nota.md"]
    typed = app_bundle.get("/api/files/search", params={"q": "type:video"}, headers=auth_headers)
    assert typed.status_code == 200
    assert [item["name"] for item in typed.json()["items"]] == ["video.mp4"]


def test_download_and_range(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/download", params={"path": "hola.txt"}, headers=auth_headers)
    assert response.status_code == 200
    assert response.content == b"hola mundo"
    assert response.headers["accept-ranges"] == "bytes"
    partial = app_bundle.get(
        "/api/files/download",
        params={"path": "hola.txt"},
        headers={**auth_headers, "Range": "bytes=0-3"},
    )
    assert partial.status_code == 206
    assert partial.content == b"hola"
    assert partial.headers["content-range"] == "bytes 0-3/10"


def test_upload_direct_and_conflict_rename(app_bundle, auth_headers):
    files = {"file": ("subida.txt", b"abc")}
    first = app_bundle.post("/api/files/upload", files=files, data={"target_dir": ""}, headers=auth_headers)
    assert first.status_code == 201, first.text
    assert first.json()["name"] == "subida.txt"
    files_again = {"file": ("subida.txt", b"def")}
    second = app_bundle.post("/api/files/upload", files=files_again, data={"target_dir": ""}, headers=auth_headers)
    assert second.status_code == 201
    assert second.json()["name"] == "subida_1.txt"
    assert (app_bundle.base_dir / "subida_1.txt").read_bytes() == b"def"


def test_chunked_upload_roundtrip(app_bundle, auth_headers):
    payload = bytes(range(256)) * 2400
    init = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "grande.bin", "size": len(payload), "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    assert init.status_code == 200, init.text
    info = init.json()
    assert info["chunk_count"] == 3
    offset = 0
    for index in range(info["chunk_count"]):
        chunk = payload[offset : offset + info["chunk_size"]]
        response = app_bundle.put(
            f"/api/files/upload/{info['upload_id']}/{index}",
            content=chunk,
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        offset += len(chunk)
    complete = app_bundle.post(f"/api/files/upload/{info['upload_id']}/complete", headers=auth_headers)
    assert complete.status_code == 201, complete.text
    assert (app_bundle.base_dir / "grande.bin").read_bytes() == payload


def test_chunked_upload_incomplete(app_bundle, auth_headers):
    init = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "incompleto.bin", "size": 600 * 1024, "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    info = init.json()
    app_bundle.put(
        f"/api/files/upload/{info['upload_id']}/0",
        content=b"x" * (256 * 1024),
        headers=auth_headers,
    )
    complete = app_bundle.post(f"/api/files/upload/{info['upload_id']}/complete", headers=auth_headers)
    assert complete.status_code == 400


def test_read_write_edit(app_bundle, auth_headers):
    read = app_bundle.get("/api/files/read", params={"path": "docs/nota.md"}, headers=auth_headers)
    assert read.status_code == 200
    assert "contenido" in read.json()["content"]
    write = app_bundle.put(
        "/api/files/write",
        json={"path": "docs/nota.md", "content": "# cambiado"},
        headers=auth_headers,
    )
    assert write.status_code == 200
    assert (app_bundle.base_dir / "docs" / "nota.md").read_text(encoding="utf-8") == "# cambiado"


def test_mkdir_rename_delete(app_bundle, auth_headers):
    created = app_bundle.post(
        "/api/files/mkdir",
        json={"path": "", "name": "nueva"},
        headers=auth_headers,
    )
    assert created.status_code == 201
    renamed = app_bundle.put(
        "/api/files/rename",
        json={"path": "nueva", "new_name": "renombrada"},
        headers=auth_headers,
    )
    assert renamed.status_code == 200
    assert (app_bundle.base_dir / "renombrada").is_dir()
    deleted = app_bundle.delete("/api/files", params={"path": "renombrada"}, headers=auth_headers)
    assert deleted.status_code == 200
    assert not (app_bundle.base_dir / "renombrada").exists()


def test_zip_streaming(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/zip", params={"path": "docs"}, headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content[:2] == b"PK"


def test_chunked_upload_can_resume(app_bundle, auth_headers):
    payload = b"0123456789abcdef" * (32 * 1024)
    init = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "reanudable.bin", "size": len(payload), "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    info = init.json()
    assert info["chunk_count"] == 2
    second = app_bundle.put(
        f"/api/files/upload/{info['upload_id']}/1",
        content=payload[256 * 1024 :],
        headers=auth_headers,
    )
    assert second.status_code == 200
    status = app_bundle.get(f"/api/files/upload/{info['upload_id']}", headers=auth_headers)
    assert status.status_code == 200
    assert status.json()["received"] == [1]
    first = app_bundle.put(
        f"/api/files/upload/{info['upload_id']}/0",
        content=payload[: 256 * 1024],
        headers=auth_headers,
    )
    assert first.status_code == 200
    complete = app_bundle.post(f"/api/files/upload/{info['upload_id']}/complete", headers=auth_headers)
    assert complete.status_code == 201, complete.text
    assert (app_bundle.base_dir / "reanudable.bin").read_bytes() == payload


def test_chunk_larger_than_expected_rejected(app_bundle, auth_headers):
    init = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "bloques.bin", "size": 256 * 1024, "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    info = init.json()
    response = app_bundle.put(
        f"/api/files/upload/{info['upload_id']}/0",
        content=b"x" * (256 * 1024 + 1),
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_suffix_range_and_invalid_range(app_bundle, auth_headers):
    suffix = app_bundle.get(
        "/api/files/download",
        params={"path": "hola.txt"},
        headers={**auth_headers, "Range": "bytes=-4"},
    )
    assert suffix.status_code == 206
    assert suffix.content == b"undo"
    invalid = app_bundle.get(
        "/api/files/download",
        params={"path": "hola.txt"},
        headers={**auth_headers, "Range": "bytes=99-100"},
    )
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == "bytes */10"


def test_fuzzy_search_tolerates_typos(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/search", params={"q": "vidoe"}, headers=auth_headers)
    assert response.status_code == 200
    assert any(item["name"] == "video.mp4" for item in response.json()["items"])


def test_size_filter_excludes_directories(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/search", params={"q": "size:>1b"}, headers=auth_headers)
    assert response.status_code == 200
    items = response.json()["items"]
    assert items
    assert all(not item["is_dir"] for item in items)


def test_chunked_upload_rejects_when_disk_is_full(app_bundle, auth_headers, monkeypatch):
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("vortex.backend.transfer.shutil.disk_usage", lambda path: usage(1000, 900, 100))
    response = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "no-cabe.bin", "size": 1024 * 1024, "target_dir": ""},
        headers=auth_headers,
    )
    assert response.status_code == 507
