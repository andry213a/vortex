# Pruebas de sincronización de cambios entre clientes.

from __future__ import annotations


def test_file_change_feed_tracks_create_edit_rename_and_delete(app_bundle):
    inicial = app_bundle.get("/api/system/changes", params={"since": 0})
    assert inicial.status_code == 200
    revision_inicial = inicial.json()["revision"]

    creado = app_bundle.post("/api/files/mkdir", json={"path": "docs", "name": "sync"})
    assert creado.status_code == 201
    escrito = app_bundle.put(
        "/api/files/write",
        json={"path": "docs/sync/nota.txt", "content": "primera versión"},
    )
    assert escrito.status_code == 200
    renombrado = app_bundle.put(
        "/api/files/rename",
        json={"path": "docs/sync/nota.txt", "new_name": "renombrada.txt"},
    )
    assert renombrado.status_code == 200
    eliminado = app_bundle.delete("/api/files", params={"path": "docs/sync"})
    assert eliminado.status_code == 200

    resultado = app_bundle.get(
        "/api/system/changes", params={"since": revision_inicial}
    )
    assert resultado.status_code == 200
    carga = resultado.json()
    assert carga["revision"] == revision_inicial + 4
    assert [cambio["action"] for cambio in carga["changes"]] == [
        "FILE_MKDIR",
        "FILE_WRITE",
        "FILE_RENAME",
        "FILE_DELETE",
    ]
    assert [cambio["paths"] for cambio in carga["changes"]] == [
        ["docs"],
        ["docs/sync"],
        ["docs/sync"],
        ["docs"],
    ]
    assert carga["changes"][-1]["removed"] == "docs/sync"
