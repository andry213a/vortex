from __future__ import annotations

import json


def test_path_traversal_listing_blocked(app_bundle, auth_headers):
    response = app_bundle.get("/api/files", params={"path": "../../etc"}, headers=auth_headers)
    assert response.status_code == 403


def test_path_traversal_download_blocked(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/download", params={"path": "../secreto.txt"}, headers=auth_headers)
    assert response.status_code == 403


def test_absolute_path_escape_blocked(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/stat", params={"path": "/etc/passwd"}, headers=auth_headers)
    assert response.status_code == 403


def test_null_byte_blocked(app_bundle, auth_headers):
    response = app_bundle.get("/api/files/stat", params={"path": "hola.txt\x00.png"}, headers=auth_headers)
    assert response.status_code == 403


def test_path_traversal_is_audited(app_bundle, auth_headers):
    app_bundle.get("/api/files", params={"path": "../fuera"}, headers=auth_headers)
    log_file = app_bundle.data_dir / "logs" / "audit.log"
    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(record["action"] == "PATH_TRAVERSAL" and record["severity"] == "SECURITY" for record in records)


def test_read_only_guest_cannot_write(tmp_path, base_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from vortex.backend.app import create_app
    from vortex.config import ServerConfig

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: data_dir)
    config = ServerConfig(
        base_dir=str(base_dir),
        port=8098,
        auth_mode="none",
        guest_write=False,
    )
    app = create_app(config)
    with TestClient(app) as client:
        listing = client.get("/api/files", params={"path": ""})
        assert listing.status_code == 200
        response = client.post("/api/files/mkdir", json={"path": "", "name": "prohibida"})
        assert response.status_code == 403
        assert not (base_dir / "prohibida").exists()


def test_rate_limiter_status_does_not_deadlock():
    from vortex.backend.security import RateLimiter

    limiter = RateLimiter(max_attempts=2, block_seconds=60)
    assert limiter.status("10.0.0.1")["blocked"] is False
    assert limiter.record_failure("10.0.0.1") is False
    assert limiter.record_failure("10.0.0.1") is True
    status = limiter.status("10.0.0.1")
    assert status["blocked"] is True
    assert status["remaining"] > 0
    limiter.reset("10.0.0.1")
    assert limiter.status("10.0.0.1")["blocked"] is False


def test_guest_write_allows_writes(tmp_path, base_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from vortex.backend.app import create_app
    from vortex.config import ServerConfig

    data_dir = tmp_path / "data-guest"
    data_dir.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: data_dir)
    config = ServerConfig(
        base_dir=str(base_dir),
        port=8095,
        auth_mode="none",
        guest_write=True,
    )
    app = create_app(config)
    with TestClient(app) as client:
        response = client.post("/api/files/mkdir", json={"path": "", "name": "permitida"})
        assert response.status_code == 201
        assert (base_dir / "permitida").is_dir()


def test_log_standalone_writes_privilege_event(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr("vortex.config.app_data_dir", lambda: tmp_path)
    from vortex.backend.audit import WARNING, log_standalone

    record = log_standalone(
        action="PRIVILEGE_REQUEST",
        severity=WARNING,
        target_path="/ruta/protegida",
        status_code=200,
        details={"requested": True},
    )
    assert record["action"] == "PRIVILEGE_REQUEST"
    log_file = tmp_path / "logs" / "audit.log"
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines[-1]["action"] == "PRIVILEGE_REQUEST"
    assert lines[-1]["severity"] == "WARNING"
