from __future__ import annotations

import json

from vortex.config import ConfigStore, ServerConfig


def test_guest_write_defaults_to_full_control():
    assert ServerConfig().guest_write is True


def test_validate_keeps_read_only_guest_setting(tmp_path):
    config = ServerConfig(base_dir=str(tmp_path), guest_write=False)
    config.validate()
    assert config.guest_write is False


def test_config_store_persists_guest_write(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = ServerConfig(base_dir=str(tmp_path), guest_write=False)
    config.validate()
    store.save(config)

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["guest_write"] is False
    assert store.load().guest_write is False


def test_legacy_config_without_guest_write_keeps_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"base_dir": str(tmp_path)}), encoding="utf-8")
    config = ConfigStore(path).load()
    assert config.guest_write is True
