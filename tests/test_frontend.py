# Pruebas de la ventana Flet y del arranque en modo navegador.

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
    pagina = _StubPage(web=False)
    VortexUI(pagina).start()

    assert pagina.window.title_bar_hidden is True
    assert pagina.window.title_bar_buttons_hidden is True
    raiz = pagina.controls[0]
    assert isinstance(raiz, ft.Column)
    barra_titulo = raiz.controls[0]
    assert isinstance(barra_titulo, ft.Container)
    assert barra_titulo.height == 46
    assert barra_titulo.bgcolor == "#111827"
    fila = barra_titulo.content
    assert isinstance(fila, ft.Row)
    assert isinstance(fila.controls[0], ft.WindowDragArea)
    controles = fila.controls[1]
    assert isinstance(controles, ft.Row)
    assert len(controles.controls) == 4
    assert controles.controls[0].tooltip == "Personalizar colores"


def test_web_mode_keeps_native_page_root() -> None:
    pagina = _StubPage(web=True)
    VortexUI(pagina).start()

    assert pagina.window.title_bar_hidden is False
    assert not isinstance(pagina.controls[0], ft.Column)


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
    llamadas: list[dict] = []
    monkeypatch.setattr(frontend_app, "_libmpv_available", lambda: False)
    monkeypatch.setattr(frontend_app.ft, "app", lambda **kwargs: llamadas.append(kwargs))

    frontend_app.run_app()

    assert len(llamadas) == 1
    assert llamadas[0]["view"] == ft.AppView.WEB_BROWSER
    assert "libmpv.so.1" in capsys.readouterr().err


def test_flet_web_compat_ignores_removed_openapi_prefix(monkeypatch) -> None:
    import fastapi

    vistos: dict = {}

    def fake_init(self, *args, **kwargs) -> None:
        vistos.update(kwargs)

    monkeypatch.setattr(fastapi.FastAPI, "__init__", fake_init)
    frontend_app._enable_flet_web_compat()
    fastapi.FastAPI.__init__(object(), openapi_prefix="/vortex", debug=True)

    assert "openapi_prefix" not in vistos
    assert vistos["debug"] is True


def test_run_app_uses_native_window_when_libmpv_exists(monkeypatch) -> None:
    llamadas: list[dict] = []
    monkeypatch.setattr(frontend_app, "_libmpv_available", lambda: True)
    monkeypatch.setattr(frontend_app.ft, "app", lambda **kwargs: llamadas.append(kwargs))

    frontend_app.run_app()

    assert len(llamadas) == 1
    assert "view" not in llamadas[0]
