from __future__ import annotations


def test_file_change_feed_tracks_create_edit_rename_and_delete(app_bundle):
    initial = app_bundle.get("/api/system/changes", params={"since": 0})
    assert initial.status_code == 200
    start_revision = initial.json()["revision"]

    created = app_bundle.post("/api/files/mkdir", json={"path": "docs", "name": "sync"})
    assert created.status_code == 201
    written = app_bundle.put(
        "/api/files/write",
        json={"path": "docs/sync/nota.txt", "content": "primera versión"},
    )
    assert written.status_code == 200
    renamed = app_bundle.put(
        "/api/files/rename",
        json={"path": "docs/sync/nota.txt", "new_name": "renombrada.txt"},
    )
    assert renamed.status_code == 200
    deleted = app_bundle.delete("/api/files", params={"path": "docs/sync"})
    assert deleted.status_code == 200

    result = app_bundle.get(
        "/api/system/changes", params={"since": start_revision}
    )
    assert result.status_code == 200
    payload = result.json()
    assert payload["revision"] == start_revision + 4
    assert [change["action"] for change in payload["changes"]] == [
        "FILE_MKDIR",
        "FILE_WRITE",
        "FILE_RENAME",
        "FILE_DELETE",
    ]
    assert [change["paths"] for change in payload["changes"]] == [
        ["docs"],
        ["docs/sync"],
        ["docs/sync"],
        ["docs"],
    ]
    assert payload["changes"][-1]["removed"] == "docs/sync"
