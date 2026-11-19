# Pruebas de listado, búsqueda, descargas y operaciones CRUD.

from __future__ import annotations


def test_list_and_parent(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files", params={"path": ""}, headers=auth_headers)
    assert respuesta.status_code == 200
    datos = respuesta.json()
    nombres = {elemento["name"] for elemento in datos["items"]}
    assert {"hola.txt", "docs"} <= nombres
    assert datos["parent"] is None
    documentos = app_bundle.get("/api/files", params={"path": "docs"}, headers=auth_headers)
    assert documentos.status_code == 200
    assert documentos.json()["parent"] == ""


def test_fuzzy_search_and_filters(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/search", params={"q": "nota"}, headers=auth_headers)
    assert respuesta.status_code == 200
    assert any(elemento["name"] == "nota.md" for elemento in respuesta.json()["items"])
    filtrado = app_bundle.get("/api/files/search", params={"q": "ext:md"}, headers=auth_headers)
    assert filtrado.status_code == 200
    assert [elemento["name"] for elemento in filtrado.json()["items"]] == ["nota.md"]
    tipado = app_bundle.get("/api/files/search", params={"q": "type:video"}, headers=auth_headers)
    assert tipado.status_code == 200
    assert [elemento["name"] for elemento in tipado.json()["items"]] == ["video.mp4"]


def test_download_and_range(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/download", params={"path": "hola.txt"}, headers=auth_headers)
    assert respuesta.status_code == 200
    assert respuesta.content == b"hola mundo"
    assert respuesta.headers["accept-ranges"] == "bytes"
    parcial = app_bundle.get(
        "/api/files/download",
        params={"path": "hola.txt"},
        headers={**auth_headers, "Range": "bytes=0-3"},
    )
    assert parcial.status_code == 206
    assert parcial.content == b"hola"
    assert parcial.headers["content-range"] == "bytes 0-3/10"


def test_upload_direct_and_conflict_rename(app_bundle, auth_headers):
    archivos = {"file": ("subida.txt", b"abc")}
    primero = app_bundle.post("/api/files/upload", files=archivos, data={"target_dir": ""}, headers=auth_headers)
    assert primero.status_code == 201, primero.text
    assert primero.json()["name"] == "subida.txt"
    archivos_nuevos = {"file": ("subida.txt", b"def")}
    segundo = app_bundle.post("/api/files/upload", files=archivos_nuevos, data={"target_dir": ""}, headers=auth_headers)
    assert segundo.status_code == 201
    assert segundo.json()["name"] == "subida_1.txt"
    assert (app_bundle.base_dir / "subida_1.txt").read_bytes() == b"def"


def test_chunked_upload_roundtrip(app_bundle, auth_headers):
    carga = bytes(range(256)) * 2400
    inicial = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "grande.bin", "size": len(carga), "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    assert inicial.status_code == 200, inicial.text
    informacion = inicial.json()
    assert informacion["chunk_count"] == 3
    desplazamiento = 0
    for indice in range(informacion["chunk_count"]):
        bloque = carga[desplazamiento : desplazamiento + informacion["chunk_size"]]
        respuesta = app_bundle.put(
            f"/api/files/upload/{informacion['upload_id']}/{indice}",
            content=bloque,
            headers=auth_headers,
        )
        assert respuesta.status_code == 200, respuesta.text
        desplazamiento += len(bloque)
    completar = app_bundle.post(f"/api/files/upload/{informacion['upload_id']}/complete", headers=auth_headers)
    assert completar.status_code == 201, completar.text
    assert (app_bundle.base_dir / "grande.bin").read_bytes() == carga


def test_chunked_upload_incomplete(app_bundle, auth_headers):
    inicial = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "incompleto.bin", "size": 600 * 1024, "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    informacion = inicial.json()
    app_bundle.put(
        f"/api/files/upload/{informacion['upload_id']}/0",
        content=b"x" * (256 * 1024),
        headers=auth_headers,
    )
    completar = app_bundle.post(f"/api/files/upload/{informacion['upload_id']}/complete", headers=auth_headers)
    assert completar.status_code == 400


def test_read_write_edit(app_bundle, auth_headers):
    lectura = app_bundle.get("/api/files/read", params={"path": "docs/nota.md"}, headers=auth_headers)
    assert lectura.status_code == 200
    assert "contenido" in lectura.json()["content"]
    escritura = app_bundle.put(
        "/api/files/write",
        json={"path": "docs/nota.md", "content": "# cambiado"},
        headers=auth_headers,
    )
    assert escritura.status_code == 200
    assert (app_bundle.base_dir / "docs" / "nota.md").read_text(encoding="utf-8") == "# cambiado"


def test_mkdir_rename_delete(app_bundle, auth_headers):
    creado = app_bundle.post(
        "/api/files/mkdir",
        json={"path": "", "name": "nueva"},
        headers=auth_headers,
    )
    assert creado.status_code == 201
    renombrado = app_bundle.put(
        "/api/files/rename",
        json={"path": "nueva", "new_name": "renombrada"},
        headers=auth_headers,
    )
    assert renombrado.status_code == 200
    assert (app_bundle.base_dir / "renombrada").is_dir()
    eliminado = app_bundle.delete("/api/files", params={"path": "renombrada"}, headers=auth_headers)
    assert eliminado.status_code == 200
    assert not (app_bundle.base_dir / "renombrada").exists()


def test_zip_streaming(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/zip", params={"path": "docs"}, headers=auth_headers)
    assert respuesta.status_code == 200
    assert respuesta.headers["content-type"] == "application/zip"
    assert respuesta.content[:2] == b"PK"


def test_chunked_upload_can_resume(app_bundle, auth_headers):
    carga = b"0123456789abcdef" * (32 * 1024)
    inicial = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "reanudable.bin", "size": len(carga), "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    informacion = inicial.json()
    assert informacion["chunk_count"] == 2
    segundo = app_bundle.put(
        f"/api/files/upload/{informacion['upload_id']}/1",
        content=carga[256 * 1024 :],
        headers=auth_headers,
    )
    assert segundo.status_code == 200
    estado = app_bundle.get(f"/api/files/upload/{informacion['upload_id']}", headers=auth_headers)
    assert estado.status_code == 200
    assert estado.json()["received"] == [1]
    primero = app_bundle.put(
        f"/api/files/upload/{informacion['upload_id']}/0",
        content=carga[: 256 * 1024],
        headers=auth_headers,
    )
    assert primero.status_code == 200
    completar = app_bundle.post(f"/api/files/upload/{informacion['upload_id']}/complete", headers=auth_headers)
    assert completar.status_code == 201, completar.text
    assert (app_bundle.base_dir / "reanudable.bin").read_bytes() == carga


def test_chunk_larger_than_expected_rejected(app_bundle, auth_headers):
    inicial = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "bloques.bin", "size": 256 * 1024, "target_dir": "", "chunk_size": 256 * 1024},
        headers=auth_headers,
    )
    informacion = inicial.json()
    respuesta = app_bundle.put(
        f"/api/files/upload/{informacion['upload_id']}/0",
        content=b"x" * (256 * 1024 + 1),
        headers=auth_headers,
    )
    assert respuesta.status_code == 400


def test_suffix_range_and_invalid_range(app_bundle, auth_headers):
    sufijo = app_bundle.get(
        "/api/files/download",
        params={"path": "hola.txt"},
        headers={**auth_headers, "Range": "bytes=-4"},
    )
    assert sufijo.status_code == 206
    assert sufijo.content == b"undo"
    invalido = app_bundle.get(
        "/api/files/download",
        params={"path": "hola.txt"},
        headers={**auth_headers, "Range": "bytes=99-100"},
    )
    assert invalido.status_code == 416
    assert invalido.headers["content-range"] == "bytes */10"


def test_fuzzy_search_tolerates_typos(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/search", params={"q": "vidoe"}, headers=auth_headers)
    assert respuesta.status_code == 200
    assert any(elemento["name"] == "video.mp4" for elemento in respuesta.json()["items"])


def test_size_filter_excludes_directories(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/search", params={"q": "size:>1b"}, headers=auth_headers)
    assert respuesta.status_code == 200
    elementos = respuesta.json()["items"]
    assert elementos
    assert all(not elemento["is_dir"] for elemento in elementos)


def test_chunked_upload_rejects_when_disk_is_full(app_bundle, auth_headers, monkeypatch):
    from collections import namedtuple

    uso = namedtuple("usage", "total used free")
    monkeypatch.setattr("vortex.backend.transfer.shutil.disk_usage", lambda ruta: uso(1000, 900, 100))
    respuesta = app_bundle.post(
        "/api/files/upload/init",
        json={"filename": "no-cabe.bin", "size": 1024 * 1024, "target_dir": ""},
        headers=auth_headers,
    )
    assert respuesta.status_code == 507
