# Fixtures compartidas: árbol temporal, configuración y cliente autenticado.

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vortex.backend.app import create_app
from vortex.config import ServerConfig


@pytest.fixture()
def base_dir(tmp_path):
    raiz = tmp_path / "base"
    raiz.mkdir()
    (raiz / "docs").mkdir()
    (raiz / "hola.txt").write_text("hola mundo", encoding="utf-8")
    (raiz / "docs" / "nota.md").write_text("# nota\ncontenido", encoding="utf-8")
    (raiz / "docs" / "video.mp4").write_bytes(b"fake-video")
    return raiz


@pytest.fixture()
def app_bundle(tmp_path, base_dir, monkeypatch):
    directorio_datos = tmp_path / "data"
    directorio_datos.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: directorio_datos)
    configuracion = ServerConfig(
        base_dir=str(base_dir),
        port=8099,
        auth_mode="auto",
        auto_token="TEST-1234",
        chunk_size=256 * 1024,
    )
    aplicacion = create_app(configuracion)
    with TestClient(aplicacion) as cliente:
        cliente.base_dir = base_dir
        cliente.config = configuracion
        cliente.runtime = aplicacion.state.runtime
        cliente.data_dir = directorio_datos
        yield cliente


@pytest.fixture()
def auth_headers(app_bundle):
    respuesta = app_bundle.post("/api/auth/login", json={"secret": "TEST-1234"})
    assert respuesta.status_code == 200, respuesta.text
    token = respuesta.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
