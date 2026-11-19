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
    for handler in list(logger._logger.handlers):
        handler.close()
        logger._logger.removeHandler(handler)


def test_log_writes_json_schema(tmp_path):
    logger = _logger(tmp_path)
    try:
        record = logger.log(
            action="FILE_UPLOAD",
            severity=SECURITY,
            client_ip="192.168.1.20",
            user_agent="pytest",
            target_path="docs/nota.md",
            status_code=201,
            details={"size": 42, "mode": "chunked"},
        )
        assert set(record) == FIELDS
        assert record["severity"] == SECURITY
        assert record["status_code"] == 201
        assert record["details"] == {"size": 42, "mode": "chunked"}
        assert len(record["event_id"]) == 12
        assert record["timestamp"].endswith("+00:00")

        lines = logger.log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == record
    finally:
        _close(logger)


def test_log_defaults_and_custom_event_id(tmp_path):
    logger = _logger(tmp_path)
    try:
        record = logger.log(action="SERVER_START", event_id="evento-fijo")
        assert record["event_id"] == "evento-fijo"
        assert record["severity"] == INFO
        assert record["client_ip"] == ""
        assert record["details"] == {}
        empty = logger.log(action="SERVER_STOP")
        assert empty["details"] == {}
        assert empty["details"] is not record["details"]
        assert logger.log(action="LOGIN_FAILED", severity=WARNING)["severity"] == WARNING
    finally:
        _close(logger)


def test_listeners_receive_records_and_can_unsubscribe(tmp_path):
    logger = _logger(tmp_path)
    received: list[dict] = []
    remove = logger.add_listener(received.append)
    try:
        first = logger.log(action="LOGIN_SUCCESS")
        assert received == [first]
        remove()
        logger.log(action="LOGIN_FAILED")
        assert received == [first]
        remove()
    finally:
        _close(logger)


def test_listener_errors_do_not_break_logging(tmp_path):
    logger = _logger(tmp_path)
    received: list[dict] = []

    def broken(record: dict) -> None:
        raise RuntimeError("listener roto")

    logger.add_listener(broken)
    logger.add_listener(received.append)
    try:
        record = logger.log(action="FILE_DELETE")
        assert received == [record]
    finally:
        _close(logger)


def test_tail_returns_last_records_and_ignores_corrupt_lines(tmp_path):
    logger = _logger(tmp_path)
    try:
        for index in range(5):
            logger.log(action=f"EVENTO_{index}")
        tail = logger.tail(2)
        assert [item["action"] for item in tail] == ["EVENTO_3", "EVENTO_4"]
        with logger.log_file.open("a", encoding="utf-8") as handle:
            handle.write("esto no es json\n")
        assert [item["action"] for item in logger.tail(3)] == ["EVENTO_3", "EVENTO_4"]
        assert logger.tail(10)
    finally:
        _close(logger)


def test_tail_without_file(tmp_path):
    logger = AuditLogger(tmp_path / "inexistente" / "audit.log")
    try:
        logger.log_file.unlink(missing_ok=True)
        assert logger.tail() == []
    finally:
        _close(logger)


def test_rotation_creates_bounded_backups(tmp_path, monkeypatch):
    monkeypatch.setattr("vortex.backend.audit.MAX_AUDIT_BYTES", 300)
    monkeypatch.setattr("vortex.backend.audit.AUDIT_BACKUPS", 2)
    logger = AuditLogger(tmp_path / "audit.log")
    try:
        for index in range(20):
            logger.log(action="FILE_WRITE", details={"payload": "x" * 120, "index": index})
        assert logger.log_file.exists()
        assert (tmp_path / "audit.log.1").exists()
        assert (tmp_path / "audit.log.2").exists()
        assert not (tmp_path / "audit.log.3").exists()
    finally:
        _close(logger)


def test_log_standalone_uses_app_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("vortex.config.app_data_dir", lambda: tmp_path)
    record = log_standalone(action="SERVER_START", status_code=200, details={"port": 8080})
    assert record["action"] == "SERVER_START"
    log_file = tmp_path / "logs" / "audit.log"
    assert log_file.is_file()
    assert json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1]) == record
