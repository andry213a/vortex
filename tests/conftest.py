from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vortex.backend.app import create_app
from vortex.config import ServerConfig


@pytest.fixture()
def base_dir(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (base / "docs").mkdir()
    (base / "hola.txt").write_text("hola mundo", encoding="utf-8")
    (base / "docs" / "nota.md").write_text("# nota\ncontenido", encoding="utf-8")
    (base / "docs" / "video.mp4").write_bytes(b"fake-video")
    return base


@pytest.fixture()
def app_bundle(tmp_path, base_dir, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: data_dir)
    config = ServerConfig(
        base_dir=str(base_dir),
        port=8099,
        auth_mode="auto",
        auto_token="TEST-1234",
        chunk_size=256 * 1024,
    )
    app = create_app(config)
    with TestClient(app) as client:
        client.base_dir = base_dir
        client.config = config
        client.runtime = app.state.runtime
        client.data_dir = data_dir
        yield client


@pytest.fixture()
def auth_headers(app_bundle):
    response = app_bundle.post("/api/auth/login", json={"secret": "TEST-1234"})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
