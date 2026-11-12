from __future__ import annotations

import json


def test_status_is_public(app_bundle):
    response = app_bundle.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["auth_mode"] == "auto"
    assert data["requires_login"] is True


def test_requires_auth_before_listing(app_bundle):
    response = app_bundle.get("/api/files", params={"path": ""})
    assert response.status_code == 401
    assert "hola.txt" not in response.text


def test_login_success_and_me(app_bundle):
    login = app_bundle.post("/api/auth/login", json={"secret": "TEST-1234"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    me = app_bundle.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["role"] == "host"


def test_login_failure_and_rate_limit(app_bundle):
    for attempt in range(4):
        response = app_bundle.post("/api/auth/login", json={"secret": "incorrecto"})
        assert response.status_code == 401, (attempt, response.text)
    blocked = app_bundle.post("/api/auth/login", json={"secret": "incorrecto"})
    assert blocked.status_code == 429
    still_blocked = app_bundle.post("/api/auth/login", json={"secret": "TEST-1234"})
    assert still_blocked.status_code == 429


def test_rate_limit_audit_events(app_bundle):
    for _ in range(5):
        app_bundle.post("/api/auth/login", json={"secret": "nope"})
    log_file = app_bundle.data_dir / "logs" / "audit.log"
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    actions = {record["action"] for record in lines}
    assert "LOGIN_FAILED" in actions
    assert "RATE_LIMIT_BLOCK" in actions


def test_unauthorized_is_audited(app_bundle):
    app_bundle.get("/api/files", params={"path": ""})
    log_file = app_bundle.data_dir / "logs" / "audit.log"
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(record["action"] == "UNAUTHORIZED" for record in lines)


def test_manual_password_auth(tmp_path, base_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from vortex.backend.app import create_app
    from vortex.config import ServerConfig

    data_dir = tmp_path / "data-manual"
    data_dir.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: data_dir)
    config = ServerConfig(
        base_dir=str(base_dir),
        port=8096,
        auth_mode="manual",
        manual_password="clave-segura",
    )
    app = create_app(config)
    with TestClient(app) as client:
        bad = client.post("/api/auth/login", json={"secret": "otra"})
        assert bad.status_code == 401
        good = client.post("/api/auth/login", json={"secret": "clave-segura"})
        assert good.status_code == 200
        token = good.json()["access_token"]
        listing = client.get("/api/files", headers={"Authorization": f"Bearer {token}"})
        assert listing.status_code == 200
