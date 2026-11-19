# Pruebas de persistencia de la configuración del servidor.

from __future__ import annotations

import json

from vortex.config import ConfigStore, ServerConfig


def test_guest_write_defaults_to_full_control():
    assert ServerConfig().guest_write is True


def test_validate_keeps_read_only_guest_setting(tmp_path):
    configuracion = ServerConfig(base_dir=str(tmp_path), guest_write=False)
    configuracion.validate()
    assert configuracion.guest_write is False


def test_config_store_persists_guest_write(tmp_path):
    almacen = ConfigStore(tmp_path / "config.json")
    configuracion = ServerConfig(base_dir=str(tmp_path), guest_write=False)
    configuracion.validate()
    almacen.save(configuracion)

    crudo = json.loads(almacen.path.read_text(encoding="utf-8"))
    assert crudo["guest_write"] is False
    assert almacen.load().guest_write is False


def test_legacy_config_without_guest_write_keeps_default(tmp_path):
    ruta = tmp_path / "config.json"
    ruta.write_text(json.dumps({"base_dir": str(tmp_path)}), encoding="utf-8")
    configuracion = ConfigStore(ruta).load()
    assert configuracion.guest_write is True
