# Pruebas de path traversal, permisos y auditoría de seguridad.

from __future__ import annotations

import json


def test_path_traversal_listing_blocked(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files", params={"path": "../../etc"}, headers=auth_headers)
    assert respuesta.status_code == 403


def test_path_traversal_download_blocked(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/download", params={"path": "../secreto.txt"}, headers=auth_headers)
    assert respuesta.status_code == 403


def test_absolute_path_escape_blocked(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/stat", params={"path": "/etc/passwd"}, headers=auth_headers)
    assert respuesta.status_code == 403


def test_null_byte_blocked(app_bundle, auth_headers):
    respuesta = app_bundle.get("/api/files/stat", params={"path": "hola.txt\x00.png"}, headers=auth_headers)
    assert respuesta.status_code == 403


def test_path_traversal_is_audited(app_bundle, auth_headers):
    app_bundle.get("/api/files", params={"path": "../fuera"}, headers=auth_headers)
    archivo_log = app_bundle.data_dir / "logs" / "audit.log"
    registros = [json.loads(linea) for linea in archivo_log.read_text(encoding="utf-8").splitlines() if linea.strip()]
    assert any(registro["action"] == "PATH_TRAVERSAL" and registro["severity"] == "SECURITY" for registro in registros)


def test_guests_respect_read_only_setting(tmp_path, base_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from vortex.backend.app import create_app
    from vortex.config import ServerConfig

    directorio_datos = tmp_path / "data"
    directorio_datos.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: directorio_datos)
    configuracion = ServerConfig(
        base_dir=str(base_dir),
        port=8098,
        auth_mode="none",
        allow_guest=False,
        guest_write=False,
    )
    aplicacion = create_app(configuracion)
    with TestClient(aplicacion) as cliente:
        estado = cliente.get("/api/auth/status").json()
        assert estado["allow_guest"] is True
        assert estado["guest_write"] is False
        listado = cliente.get("/api/files", params={"path": ""})
        assert listado.status_code == 200
        lectura = cliente.get("/api/files/read", params={"path": "hola.txt"})
        assert lectura.status_code == 200
        respuesta = cliente.post("/api/files/mkdir", json={"path": "", "name": "denegada"})
        assert respuesta.status_code == 403
        assert not (base_dir / "denegada").exists()


def test_rate_limiter_status_does_not_deadlock():
    from vortex.backend.security import RateLimiter

    limitador = RateLimiter(max_attempts=2, block_seconds=60)
    assert limitador.status("10.0.0.1")["blocked"] is False
    assert limitador.record_failure("10.0.0.1") is False
    assert limitador.record_failure("10.0.0.1") is True
    estado = limitador.status("10.0.0.1")
    assert estado["blocked"] is True
    assert estado["remaining"] > 0
    limitador.reset("10.0.0.1")
    assert limitador.status("10.0.0.1")["blocked"] is False


def test_guests_can_write_by_default(tmp_path, base_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from vortex.backend.app import create_app
    from vortex.config import ServerConfig

    directorio_datos = tmp_path / "data-guest"
    directorio_datos.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: directorio_datos)
    configuracion = ServerConfig(
        base_dir=str(base_dir),
        port=8095,
        auth_mode="none",
    )
    aplicacion = create_app(configuracion)
    with TestClient(aplicacion) as cliente:
        respuesta = cliente.post("/api/files/mkdir", json={"path": "", "name": "permitida"})
        assert respuesta.status_code == 201
        assert (base_dir / "permitida").is_dir()


def test_log_standalone_writes_privilege_event(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr("vortex.config.app_data_dir", lambda: tmp_path)
    from vortex.backend.audit import WARNING, log_standalone

    registro = log_standalone(
        action="PRIVILEGE_REQUEST",
        severity=WARNING,
        target_path="/ruta/protegida",
        status_code=200,
        details={"requested": True},
    )
    assert registro["action"] == "PRIVILEGE_REQUEST"
    archivo_log = tmp_path / "logs" / "audit.log"
    lineas = [json.loads(linea) for linea in archivo_log.read_text(encoding="utf-8").splitlines() if linea.strip()]
    assert lineas[-1]["action"] == "PRIVILEGE_REQUEST"
    assert lineas[-1]["severity"] == "WARNING"
