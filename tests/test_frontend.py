from __future__ import annotations

import flet as ft

from vortex.frontend import app as frontend_app
from vortex.frontend.app import VortexUI


class _Window:
    width = 0
    height = 0
    min_width = 0
    min_height = 0
    bgcolor = None
    title_bar_hidden = False
    title_bar_buttons_hidden = False
    minimizable = True
    maximizable = True
    resizable = True
    minimized = False
    maximized = False

    def close(self) -> None:
        pass


class _StubPage:
    def __init__(self, web: bool = False) -> None:
        self.title = ""
        self.theme_mode = None
        self.theme = None
        self.bgcolor = None
        self.padding = None
        self.spacing = None
        self.web = web
        self.window = _Window()
        self.on_keyboard_event = None
        self.on_disconnect = None
        self.overlay: list = []
        self.controls: list = []

    def update(self) -> None:
        pass

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def open(self, control) -> None:
        pass

    def close(self, control) -> None:
        pass

    def run_task(self, handler, *args, **kwargs):
        return None


def test_desktop_uses_custom_themed_window() -> None:
    page = _StubPage(web=False)
    VortexUI(page).start()

    assert page.window.title_bar_hidden is True
    assert page.window.title_bar_buttons_hidden is True
    root = page.controls[0]
    assert isinstance(root, ft.Column)
    title_bar = root.controls[0]
    assert isinstance(title_bar, ft.Container)
    assert title_bar.height == 46
    assert title_bar.bgcolor == "#111827"
    row = title_bar.content
    assert isinstance(row, ft.Row)
    assert isinstance(row.controls[0], ft.WindowDragArea)
    controls = row.controls[1]
    assert isinstance(controls, ft.Row)
    assert len(controls.controls) == 4
    assert controls.controls[0].tooltip == "Personalizar colores"


def test_web_mode_keeps_native_page_root() -> None:
    page = _StubPage(web=True)
    VortexUI(page).start()

    assert page.window.title_bar_hidden is False
    assert not isinstance(page.controls[0], ft.Column)


def test_libmpv_check_skips_non_linux(monkeypatch) -> None:
    monkeypatch.setattr(frontend_app.sys, "platform", "win32")
    assert frontend_app._libmpv_available() is True


def test_libmpv_check_detects_missing_library(monkeypatch) -> None:
    import ctypes

    def fail(name):
        raise OSError(name)

    monkeypatch.setattr(frontend_app.sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", fail)

    assert frontend_app._libmpv_available() is False


def test_run_app_falls_back_to_browser_without_libmpv(monkeypatch, capsys) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(frontend_app, "_libmpv_available", lambda: False)
    monkeypatch.setattr(frontend_app.ft, "app", lambda **kwargs: calls.append(kwargs))

    frontend_app.run_app()

    assert len(calls) == 1
    assert calls[0]["view"] == ft.AppView.WEB_BROWSER
    assert "libmpv.so.1" in capsys.readouterr().err


def test_flet_web_compat_ignores_removed_openapi_prefix(monkeypatch) -> None:
    import fastapi

    seen: dict = {}

    def fake_init(self, *args, **kwargs) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(fastapi.FastAPI, "__init__", fake_init)
    frontend_app._enable_flet_web_compat()
    fastapi.FastAPI.__init__(object(), openapi_prefix="/vortex", debug=True)

    assert "openapi_prefix" not in seen
    assert seen["debug"] is True


def test_run_app_uses_native_window_when_libmpv_exists(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(frontend_app, "_libmpv_available", lambda: True)
    monkeypatch.setattr(frontend_app.ft, "app", lambda **kwargs: calls.append(kwargs))

    frontend_app.run_app()

    assert len(calls) == 1
    assert "view" not in calls[0]
