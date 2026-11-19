# Pruebas del registro de auditoría, listeners y rotación de archivos.

from __future__ import annotations

import json

from vortex.backend.audit import (
    INFO,
    SECURITY,
    WARNING,
    AuditLogger,
    log_standalone,
)

FIELDS = {
    "timestamp",
    "event_id",
    "severity",
    "client_ip",
    "user_agent",
    "action",
    "target_path",
    "status_code",
    "details",
}


def _logger(tmp_path) -> AuditLogger:
    return AuditLogger(tmp_path / "logs" / "audit.log")


def _close(logger: AuditLogger) -> None:
    for manejador in list(logger._logger.handlers):
        manejador.close()
        logger._logger.removeHandler(manejador)


def test_log_writes_json_schema(tmp_path):
    registrador = _logger(tmp_path)
    try:
        registro = registrador.log(
            action="FILE_UPLOAD",
            severity=SECURITY,
            client_ip="192.168.1.20",
            user_agent="pytest",
            target_path="docs/nota.md",
            status_code=201,
            details={"size": 42, "mode": "chunked"},
        )
        assert set(registro) == FIELDS
        assert registro["severity"] == SECURITY
        assert registro["status_code"] == 201
        assert registro["details"] == {"size": 42, "mode": "chunked"}
        assert len(registro["event_id"]) == 12
        assert registro["timestamp"].endswith("+00:00")

        lineas = registrador.log_file.read_text(encoding="utf-8").splitlines()
        assert len(lineas) == 1
        assert json.loads(lineas[0]) == registro
    finally:
        _close(registrador)


def test_log_defaults_and_custom_event_id(tmp_path):
    registrador = _logger(tmp_path)
    try:
        registro = registrador.log(action="SERVER_START", event_id="evento-fijo")
        assert registro["event_id"] == "evento-fijo"
        assert registro["severity"] == INFO
        assert registro["client_ip"] == ""
        assert registro["details"] == {}
        vacio = registrador.log(action="SERVER_STOP")
        assert vacio["details"] == {}
        assert vacio["details"] is not registro["details"]
        assert registrador.log(action="LOGIN_FAILED", severity=WARNING)["severity"] == WARNING
    finally:
        _close(registrador)


def test_listeners_receive_records_and_can_unsubscribe(tmp_path):
    registrador = _logger(tmp_path)
    recibidos: list[dict] = []
    desuscribir = registrador.add_listener(recibidos.append)
    try:
        primero = registrador.log(action="LOGIN_SUCCESS")
        assert recibidos == [primero]
        desuscribir()
        registrador.log(action="LOGIN_FAILED")
        assert recibidos == [primero]
        desuscribir()
    finally:
        _close(registrador)


def test_listener_errors_do_not_break_logging(tmp_path):
    registrador = _logger(tmp_path)
    recibidos: list[dict] = []

    def broken(record: dict) -> None:
        raise RuntimeError("listener roto")

    registrador.add_listener(broken)
    registrador.add_listener(recibidos.append)
    try:
        registro = registrador.log(action="FILE_DELETE")
        assert recibidos == [registro]
    finally:
        _close(registrador)


def test_tail_returns_last_records_and_ignores_corrupt_lines(tmp_path):
    registrador = _logger(tmp_path)
    try:
        for indice in range(5):
            registrador.log(action=f"EVENTO_{indice}")
        cola = registrador.tail(2)
        assert [elemento["action"] for elemento in cola] == ["EVENTO_3", "EVENTO_4"]
        with registrador.log_file.open("a", encoding="utf-8") as manejador:
            manejador.write("esto no es json\n")
        assert [elemento["action"] for elemento in registrador.tail(3)] == ["EVENTO_3", "EVENTO_4"]
        assert registrador.tail(10)
    finally:
        _close(registrador)


def test_tail_without_file(tmp_path):
    registrador = AuditLogger(tmp_path / "inexistente" / "audit.log")
    try:
        registrador.log_file.unlink(missing_ok=True)
        assert registrador.tail() == []
    finally:
        _close(registrador)


def test_rotation_creates_bounded_backups(tmp_path, monkeypatch):
    monkeypatch.setattr("vortex.backend.audit.MAX_AUDIT_BYTES", 300)
    monkeypatch.setattr("vortex.backend.audit.AUDIT_BACKUPS", 2)
    registrador = AuditLogger(tmp_path / "audit.log")
    try:
        for indice in range(20):
            registrador.log(action="FILE_WRITE", details={"payload": "x" * 120, "index": indice})
        assert registrador.log_file.exists()
        assert (tmp_path / "audit.log.1").exists()
        assert (tmp_path / "audit.log.2").exists()
        assert not (tmp_path / "audit.log.3").exists()
    finally:
        _close(registrador)


def test_log_standalone_uses_app_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("vortex.config.app_data_dir", lambda: tmp_path)
    registro = log_standalone(action="SERVER_START", status_code=200, details={"port": 8080})
    assert registro["action"] == "SERVER_START"
    archivo_log = tmp_path / "logs" / "audit.log"
    assert archivo_log.is_file()
    assert json.loads(archivo_log.read_text(encoding="utf-8").splitlines()[-1]) == registro
