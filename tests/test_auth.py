# Pruebas de autenticación, roles y rate limiting.

from __future__ import annotations

import json


def test_status_is_public(app_bundle):
    respuesta = app_bundle.get("/api/auth/status")
    assert respuesta.status_code == 200
    datos = respuesta.json()
    assert datos["auth_mode"] == "auto"
    assert datos["requires_login"] is False
    assert datos["allow_guest"] is True
    assert datos["guest_write"] is True


def test_guests_can_list_without_token(app_bundle):
    respuesta = app_bundle.get("/api/files", params={"path": ""})
    assert respuesta.status_code == 200
    assert "hola.txt" in respuesta.text


def test_login_success_and_me(app_bundle):
    inicio_sesion = app_bundle.post("/api/auth/login", json={"secret": "TEST-1234"})
    assert inicio_sesion.status_code == 200
    token = inicio_sesion.json()["access_token"]
    yo = app_bundle.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert yo.status_code == 200
    assert yo.json()["role"] == "host"


def test_login_failure_and_rate_limit(app_bundle):
    for intento in range(4):
        respuesta = app_bundle.post("/api/auth/login", json={"secret": "incorrecto"})
        assert respuesta.status_code == 401, (intento, respuesta.text)
    bloqueado = app_bundle.post("/api/auth/login", json={"secret": "incorrecto"})
    assert bloqueado.status_code == 429
    aun_bloqueado = app_bundle.post("/api/auth/login", json={"secret": "TEST-1234"})
    assert aun_bloqueado.status_code == 429


def test_rate_limit_audit_events(app_bundle):
    for _ in range(5):
        app_bundle.post("/api/auth/login", json={"secret": "nope"})
    archivo_log = app_bundle.data_dir / "logs" / "audit.log"
    lineas = [json.loads(linea) for linea in archivo_log.read_text(encoding="utf-8").splitlines() if linea.strip()]
    acciones = {registro["action"] for registro in lineas}
    assert "LOGIN_FAILED" in acciones
    assert "RATE_LIMIT_BLOCK" in acciones


def test_invalid_token_does_not_grant_host(app_bundle):
    yo = app_bundle.get("/api/auth/me", headers={"Authorization": "Bearer token-invalido"})
    assert yo.status_code == 200
    assert yo.json()["role"] == "guest_write"


def test_manual_password_auth(tmp_path, base_dir, monkeypatch):
    from fastapi.testclient import TestClient

    from vortex.backend.app import create_app
    from vortex.config import ServerConfig

    directorio_datos = tmp_path / "data-manual"
    directorio_datos.mkdir()
    monkeypatch.setattr("vortex.backend.state.app_data_dir", lambda: directorio_datos)
    configuracion = ServerConfig(
        base_dir=str(base_dir),
        port=8096,
        auth_mode="manual",
        manual_password="clave-segura",
    )
    aplicacion = create_app(configuracion)
    with TestClient(aplicacion) as cliente:
        malo = cliente.post("/api/auth/login", json={"secret": "otra"})
        assert malo.status_code == 401
        bueno = cliente.post("/api/auth/login", json={"secret": "clave-segura"})
        assert bueno.status_code == 200
        token = bueno.json()["access_token"]
        listado = cliente.get("/api/files", headers={"Authorization": f"Bearer {token}"})
        assert listado.status_code == 200
