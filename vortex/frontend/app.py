# Interfaz Flet del host: configuración, navegación, subidas y logs.

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from email.message import Message
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

import flet as ft
import httpx
import qrcode
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

from ..backend.audit import WARNING as AUDIT_WARNING
from ..backend.audit import log_standalone
from ..backend.mdns import local_ip
from ..backend.security import file_kind
from ..backend.system import needs_elevation, relaunch_elevated, suggest_free_port, validate_port
from ..config import ConfigStore, ServerConfig, app_data_dir, generate_auto_token
from ..resources import static_file
from ..server import ServerController, ServerStartError
from .theme import (
    ACCENT,
    ACCENT_STRONG,
    BG,
    BORDER,
    DANGER,
    MUTED,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TEXT,
    WARNING,
    color_for_kind,
    format_time,
    human_size,
    human_speed,
    icon_for_kind,
)


# Descubrimiento mDNS de otros servidores Vortex en la LAN.
class _VortexServiceListener(ServiceListener):
    def __init__(self, app: VortexUI) -> None:
        self.app = app

    def add_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self.app._resolve_discovered_server(zeroconf, service_type, name)

    def update_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self.app._resolve_discovered_server(zeroconf, service_type, name)

    def remove_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self.app._remove_discovered_server(name)


# Interfaz Flet del host: configuración, navegación y operaciones.
class VortexUI:
    ACCENT_PRESETS: ClassVar[dict[str, str]] = {
        "Azul Vortex": "#60A5FA",
        "Violeta": "#A78BFA",
        "Verde": "#34D399",
        "Cian": "#22D3EE",
        "Rosa": "#F472B6",
        "Ámbar": "#FBBF24",
    }

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.store = ConfigStore()
        self.config = self.store.load()
        self._set_accent_globals(self.config.accent_color)
        self.server = ServerController()
        self.base_url = ""
        self.is_remote = False
        self.remote_guest_write = True
        self.remote_download_notice: tuple[str, bool] | None = None
        self.discovery_zeroconf: Zeroconf | None = None
        self.discovery_browser: ServiceBrowser | None = None
        self.discovery_listener = _VortexServiceListener(self)
        self.discovery_lock = threading.Lock()
        self.discovered_servers: dict[str, dict[str, str]] = {}
        self.discovered_service_urls: dict[str, str] = {}
        self.discovered_servers_control: ft.Column | None = None
        self.discovered_status: ft.Text | None = None
        self.discovered_render_signature: tuple[str, ...] | None = None
        self.connect_dialog: ft.AlertDialog | None = None
        self.discovery_started_at = time.monotonic()
        self.discovery_error: str | None = None
        self.http: httpx.Client | None = None
        self.token: str | None = None
        self.role: str | None = None
        self.current_path = ""
        self.parent_path: str | None = None
        self.log_buffer: deque[dict] = deque(maxlen=800)
        self.log_unsubscribe = None
        self.logs_dialog_open = False
        self.logs_count_seen = -1
        self.upload_active = False
        self.upload_done = False
        self.poll_started = False
        self.upload_progress = 0.0
        self.upload_label = ""
        self.upload_speed = ""
        self.upload_error: str | None = None
        self.upload_cancel = threading.Event()
        self.web_upload_dir = app_data_dir() / "browser-picker"
        self.web_upload_dir.mkdir(parents=True, exist_ok=True)
        self.web_upload_queue: deque[Path] = deque()
        self.web_upload_results: list[Path] = []
        self.web_upload_remaining = 0
        self.folder_picker_path = Path.home()
        self.folder_picker_mode = "folder"
        self.folder_picker_selected: set[Path] = set()
        self.folder_picker_path_text = None
        self.folder_picker_selection_text = None
        self.folder_picker_list = None
        self.folder_picker_dialog = None
        self.upload_widget = None
        self.upload_bar = None
        self.upload_name_text = None
        self.upload_speed_text = None
        self.upload_cancel_button: ft.IconButton | None = None
        self.upload_cancellable = False
        self.upload_target_path = ""
        self.file_revision = 0
        self.file_sync_checked_at = 0.0
        self.listing = None
        self.breadcrumb = None
        self.count_text = None
        self.search_field = None
        self.logs_list = None
        self.palette_dialog = None
        self.palette_input = None
        self.palette_results = None
        self.dir_picker = ft.FilePicker()
        self.file_picker = ft.FilePicker()
        self.page.overlay.extend([self.dir_picker, self.file_picker])
        self.dir_picker.on_result = self._on_dir_picked
        self.file_picker.on_result = self._on_files_picked
        self.file_picker.on_upload = self._on_file_uploaded
        self._configure_page()

    # Configura la ventana, el tema y los controles iniciales.
    def _configure_page(self) -> None:
        self.page.title = "Vortex"
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.theme = ft.Theme(
            color_scheme=ft.ColorScheme(
                primary=ACCENT_STRONG,
                on_primary="#FFFFFF",
                secondary=ACCENT,
                on_secondary="#0B0F17",
                background=BG,
                on_background=TEXT,
                surface=SURFACE,
                on_surface=TEXT,
                surface_variant=SURFACE_ALT,
                on_surface_variant=MUTED,
                outline=BORDER,
                error=DANGER,
                on_error="#FFFFFF",
            )
        )
        self.page.bgcolor = BG
        self.page.padding = 0
        self.page.spacing = 0
        self.page.window.width = 1180
        self.page.window.height = 800
        self.page.window.min_width = 860
        self.page.window.min_height = 600
        self.is_desktop = not bool(getattr(self.page, "web", False))
        if self.is_desktop:
            self.page.window.bgcolor = BG
            nombre_icono = "vortex.ico" if sys.platform.startswith("win") else "vortex.png"
            self.page.window.icon = str(self._brand_asset_path(nombre_icono))
            self.page.window.title_bar_hidden = True
            self.page.window.title_bar_buttons_hidden = True
            self.page.window.minimizable = True
            self.page.window.maximizable = True
            self.page.window.resizable = True
        self.page.on_keyboard_event = self._on_keyboard
        self.page.on_disconnect = self._on_disconnect

    def _window_button(self, icon: str, tooltip: str, action, close: bool = False) -> ft.Container:
        color_normal = DANGER if close else MUTED
        fondo_hover = "#7F1D1D" if close else SURFACE_ALT
        boton = ft.Container(
            width=44,
            height=46,
            alignment=ft.alignment.center,
            tooltip=tooltip,
            on_click=action,
            content=ft.Icon(name=icon, size=15, color=color_normal),
        )

        def on_hover(e: ft.ControlEvent) -> None:
            entrando = str(getattr(e, "data", "")).lower() == "true"
            boton.bgcolor = fondo_hover if entrando else None
            color_icono = "#FFFFFF" if (entrando and close) else (TEXT if entrando else color_normal)
            if isinstance(boton.content, ft.Icon):
                boton.content.color = color_icono
            try:
                boton.update()
            except Exception:
                pass

        boton.on_hover = on_hover
        return boton

    def _window_controls(self) -> ft.Row:
        def minimize(e: ft.ControlEvent) -> None:
            self.page.window.minimized = True
            self.page.update()

        def toggle_maximize(e: ft.ControlEvent) -> None:
            self.page.window.maximized = not bool(self.page.window.maximized)
            self.page.update()

        def close(e: ft.ControlEvent) -> None:
            self.page.window.close()

        return ft.Row(
            [
                self._window_button(ft.icons.PALETTE, "Personalizar colores", self._show_appearance_settings),
                self._window_button(ft.icons.REMOVE, "Minimizar", minimize),
                self._window_button(ft.icons.CROP_SQUARE, "Maximizar", toggle_maximize),
                self._window_button(ft.icons.CLOSE, "Cerrar", close, close=True),
            ],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    @staticmethod
    def _set_accent_globals(color: str) -> None:
        global ACCENT, ACCENT_STRONG
        ACCENT = color.upper()
        canales = [int(ACCENT[indice:indice + 2], 16) for indice in (1, 3, 5)]
        ACCENT_STRONG = "#" + "".join(f"{round(canal * 0.78):02X}" for canal in canales)

    # Ajustes visuales: color de acento y preferencias de la ventana.
    def _show_appearance_settings(self, e: ft.ControlEvent | None = None) -> None:
        actual = self.config.accent_color
        rgb = [int(actual[indice:indice + 2], 16) for indice in (1, 3, 5)]
        vista_previa = ft.Container(width=52, height=36, border_radius=8, bgcolor=actual)
        campos_rgb = [
            self._field(etiqueta, str(valor), width=82, keyboard_type=ft.KeyboardType.NUMBER)
            for etiqueta, valor in zip(("R", "G", "B"), rgb)
        ]
        deslizadores: list[ft.Slider] = []

        def update_color() -> None:
            valores = []
            for campo in campos_rgb:
                try:
                    valor = int(campo.value or "0")
                except ValueError:
                    valor = 0
                valores.append(max(0, min(255, valor)))
            color = "#" + "".join(f"{valor:02X}" for valor in valores)
            vista_previa.bgcolor = color
            etiqueta_color.value = color
            for indice, deslizador in enumerate(deslizadores):
                deslizador.value = valores[indice]
            try:
                self.page.update()
            except Exception:
                pass

        for indice, campo in enumerate(campos_rgb):
            campo.on_change = lambda ev: update_color()

        for indice, valor in enumerate(rgb):
            deslizadores.append(
                ft.Slider(
                    min=0,
                    max=255,
                    divisions=255,
                    value=valor,
                    active_color=ACCENT,
                    on_change=lambda ev, canal=indice: set_channel(canal, ev.control.value),
                )
            )

        def set_channel(index: int, value: float) -> None:
            campos_rgb[index].value = str(round(value))
            update_color()

        def select_preset(color: str) -> None:
            for campo, valor in zip(campos_rgb, (int(color[indice:indice + 2], 16) for indice in (1, 3, 5))):
                campo.value = str(valor)
            update_color()

        def apply(e: ft.ControlEvent) -> None:
            acento_anterior, fuerte_anterior = ACCENT, ACCENT_STRONG
            self.config.accent_color = vista_previa.bgcolor
            self._set_accent_globals(self.config.accent_color)
            try:
                self.store.save(self.config)
            except OSError as excepcion:
                self._notify(f"No se pudo guardar el color: {excepcion}", True)
                return
            self._recolor_existing_controls(acento_anterior, fuerte_anterior)
            self.page.close(dialogo)
            self._notify("Color de Vortex actualizado")

        ajustes = ft.Row(
            [
                ft.Container(
                    width=30,
                    height=30,
                    border_radius=15,
                    bgcolor=color,
                    tooltip=nombre,
                    on_click=lambda ev, selected=color: select_preset(selected),
                    border=ft.border.all(2, TEXT if color == actual else BORDER),
                )
                for nombre, color in self.ACCENT_PRESETS.items()
            ],
            spacing=12,
        )
        etiqueta_color = ft.Text(actual, color=TEXT, font_family="monospace")
        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Color de la aplicación", color=TEXT),
            content=ft.Container(
                width=480,
                content=ft.Column(
                    [
                        ft.Text("Colores sugeridos", color=MUTED, size=12),
                        ajustes,
                        ft.Divider(color=BORDER),
                        ft.Text("Ajuste RGB", color=MUTED, size=12),
                        *[
                            ft.Row([ft.Text(etiqueta, width=20, color=TEXT), deslizador, campo], spacing=8)
                            for etiqueta, deslizador, campo in zip(("R", "G", "B"), deslizadores, campos_rgb)
                        ],
                        ft.Row([vista_previa, etiqueta_color]),
                    ],
                    tight=True,
                    spacing=10,
                ),
            ),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialogo)),
                ft.ElevatedButton("Aplicar", bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=apply),
            ],
        )
        self.page.open(dialogo)

    def _recolor_existing_controls(self, old_accent: str, old_strong: str) -> None:
        vistos: set[int] = set()

        def visit(control: ft.Control) -> None:
            if id(control) in vistos:
                return
            vistos.add(id(control))
            for atributo in ("color", "bgcolor", "active_color", "border_color", "focused_border_color"):
                try:
                    valor = getattr(control, atributo)
                    if valor == old_accent:
                        setattr(control, atributo, ACCENT)
                    elif valor == old_strong:
                        setattr(control, atributo, ACCENT_STRONG)
                except Exception:
                    pass
            for atributo in ("content", "controls"):
                try:
                    hijos = getattr(control, atributo)
                    if isinstance(hijos, ft.Control):
                        visit(hijos)
                    elif isinstance(hijos, list):
                        for hijo in hijos:
                            if isinstance(hijo, ft.Control):
                                visit(hijo)
                except Exception:
                    pass

        for control in self.page.controls:
            visit(control)
        for control in self.page.overlay:
            visit(control)
        try:
            self.page.theme.color_scheme.primary = ACCENT_STRONG
            self.page.theme.color_scheme.secondary = ACCENT
            self.page.update()
        except Exception:
            pass

    # Barra de título propia con controles de ventana.
    def _title_bar(self) -> ft.Container:
        en_ejecucion = self.server.running or self.is_remote
        color_estado = SUCCESS if en_ejecucion else MUTED
        texto_estado = "REMOTO" if self.is_remote else "EN LÍNEA" if en_ejecucion else "DETENIDO"
        marca = ft.Row(
            [
                ft.Container(
                    width=26,
                    height=26,
                    border_radius=8,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=self._brand_image(26),
                ),
                ft.Text("VORTEX", size=13, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Container(width=1, height=16, bgcolor=BORDER),
                ft.Text("NAS local", size=11, color=MUTED),
                ft.Container(expand=True),
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=9, vertical=4),
                    border_radius=999,
                    bgcolor=SURFACE_ALT,
                    border=ft.border.all(1, BORDER),
                    content=ft.Row(
                        [
                            ft.Container(width=6, height=6, border_radius=3, bgcolor=color_estado),
                            ft.Text(texto_estado, size=9, color=color_estado, weight=ft.FontWeight.W_600),
                        ],
                        spacing=5,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            expand=True,
        )
        area_arrastre = ft.WindowDragArea(
            content=ft.Container(content=marca, padding=ft.padding.only(left=14, right=10)),
            expand=True,
            maximizable=True,
        )
        return ft.Container(
            height=46,
            bgcolor=SURFACE,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Row(
                [area_arrastre, self._window_controls()],
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    @staticmethod
    def _brand_asset_path(filename: str = "vortex.png") -> Path:
        return static_file("img", filename)

    @classmethod
    def _brand_image(cls, size: int) -> ft.Control:
        try:
            logo_codificado = base64.b64encode(cls._brand_asset_path().read_bytes()).decode("ascii")
        except OSError:
            return ft.Icon(name=ft.icons.BOLT, size=max(16, size - 6), color=ACCENT)
        return ft.Image(
            src_base64=logo_codificado,
            width=size,
            height=size,
            fit=ft.ImageFit.CONTAIN,
        )

    def _window_shell(self, content: ft.Control) -> ft.Control:
        if not self.is_desktop:
            return content
        return ft.Column(
            [self._title_bar(), ft.Container(expand=True, content=content)],
            expand=True,
            spacing=0,
        )

    def start(self) -> None:
        self.show_setup()
        self._start_polling()
        self._start_discovery()

    # Anuncio y descubrimiento de servidores en la red local.
    def _start_discovery(self) -> None:
        if self.discovery_browser is not None:
            return
        try:
            self.discovery_zeroconf = Zeroconf()
            self.discovery_browser = ServiceBrowser(
                self.discovery_zeroconf,
                "_http._tcp.local.",
                self.discovery_listener,
            )
        except Exception as excepcion:
            self.discovery_error = str(excepcion)

    def _stop_discovery(self) -> None:
        if self.discovery_browser is not None:
            try:
                self.discovery_browser.cancel()
            except Exception:
                pass
            self.discovery_browser = None
        if self.discovery_zeroconf is not None:
            try:
                self.discovery_zeroconf.close()
            except Exception:
                pass
            self.discovery_zeroconf = None

    def _resolve_discovered_server(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        try:
            informacion = zeroconf.get_service_info(service_type, name, timeout=1500)
            if informacion is None or (informacion.properties or {}).get(b"auth") != b"vortex":
                return
            direcciones = informacion.parsed_addresses()
            if not direcciones:
                return
            direccion = next(
                (elemento for elemento in direcciones if ipaddress.ip_address(elemento).version == 4),
                direcciones[0],
            )
            direccion_formateada = f"[{direccion}]" if ipaddress.ip_address(direccion).version == 6 else direccion
            url = f"http://{direccion_formateada}:{informacion.port}"
            nombre_visible = name.split(f".{service_type}", 1)[0]
            if nombre_visible.startswith("Vortex on "):
                nombre_visible = nombre_visible.removeprefix("Vortex on ")
            with self.discovery_lock:
                url_anterior = self.discovered_service_urls.get(name)
                if url_anterior and url_anterior != url:
                    self.discovered_servers.pop(url_anterior, None)
                self.discovered_service_urls[name] = url
                self.discovered_servers[url] = {"name": nombre_visible or "Servidor Vortex", "url": url}
        except Exception:
            return

    def _remove_discovered_server(self, name: str) -> None:
        with self.discovery_lock:
            url = self.discovered_service_urls.pop(name, None)
            if url and url not in self.discovered_service_urls.values():
                self.discovered_servers.pop(url, None)

    def _refresh_discovered_servers(self) -> None:
        control = self.discovered_servers_control
        if control is None:
            return
        with self.discovery_lock:
            servidores = tuple(sorted(
                ((elemento["name"], elemento["url"]) for elemento in self.discovered_servers.values()),
                key=lambda item: (item[0].casefold(), item[1]),
            ))
        if servidores:
            mensaje_estado = f"{len(servidores)} servidor(es) · actualizado {time.strftime('%H:%M:%S')}"
        elif self.discovery_error:
            mensaje_estado = "No se pudo buscar automáticamente. Puedes escribir la IP abajo."
        elif time.monotonic() - self.discovery_started_at < 8:
            mensaje_estado = "Buscando servidores Vortex en la red local…"
        else:
            mensaje_estado = "No se encontraron servidores. Puedes escribir la IP abajo."
        firma = (mensaje_estado, *(f"{nombre}|{url}" for nombre, url in servidores))
        if firma == self.discovered_render_signature:
            return
        self.discovered_render_signature = firma
        control.controls.clear()
        estado = self.discovered_status
        if estado is not None:
            estado.value = mensaje_estado
        if servidores:
            for nombre, url in servidores:
                control.controls.append(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=10, vertical=8),
                        border_radius=8,
                        bgcolor=SURFACE_ALT,
                        on_click=lambda e, server_url=url: self._connect_discovered_server(server_url),
                        on_hover=self._hover_row,
                        content=ft.Row(
                            [
                                ft.Icon(name=ft.icons.DNS_OUTLINED, color=ACCENT, size=18),
                                ft.Column(
                                    [
                                        ft.Text(nombre, color=TEXT, size=12, weight=ft.FontWeight.W_600),
                                        ft.Text(url, color=MUTED, size=11, font_family="monospace"),
                                    ],
                                    spacing=2,
                                    expand=True,
                                ),
                                ft.Icon(name=ft.icons.ARROW_FORWARD, color=MUTED, size=16),
                            ],
                            spacing=10,
                        ),
                    )
                )
        try:
            self.page.update()
        except Exception:
            pass

    def _connect_discovered_server(self, server_url: str) -> None:
        if self.connect_dialog is not None:
            self.page.close(self.connect_dialog)
        self._connect_remote(server_url)

    def _default_directory(self) -> str:
        if self.config.base_dir:
            return self.config.base_dir
        for candidato in (Path.home() / "documento", Path.home() / "Documents", Path.home()):
            if candidato.exists():
                return str(candidato)
        return str(Path.cwd())

    def _on_disconnect(self, e: ft.ControlEvent) -> None:
        self._stop_discovery()
        try:
            self.server.stop()
        except Exception:
            pass

    def _notify(self, message: str, error: bool = False) -> None:
        aviso = ft.SnackBar(
            content=ft.Text(message, color=TEXT, size=13),
            bgcolor=DANGER if error else SURFACE,
            duration=4500,
        )
        try:
            self.page.open(aviso)
        except Exception:
            pass

    def _field(self, label: str, value: str = "", **kwargs) -> ft.TextField:
        return ft.TextField(
            label=label,
            value=value,
            text_size=13,
            color=TEXT,
            label_style=ft.TextStyle(color=MUTED, size=12),
            bgcolor=SURFACE_ALT,
            border_color=BORDER,
            focused_border_color=ACCENT,
            border_radius=10,
            content_padding=ft.padding.symmetric(horizontal=12, vertical=12),
            **kwargs,
        )

    def _card(self, content: ft.Control, width: int = 620) -> ft.Container:
        return ft.Container(
            width=width,
            bgcolor=SURFACE,
            border=ft.border.all(1, BORDER),
            border_radius=18,
            padding=32,
            content=content,
        )

    # Asistente inicial para elegir carpeta, puerto y autenticación.
    def show_setup(self) -> None:
        self.token = None
        self.role = None
        self.config = self.store.load()
        self.dir_field = self._field("Carpeta raíz del servidor", self._default_directory(), expand=True)
        self.port_field = self._field("Puerto", str(self.config.port), width=130, keyboard_type=ft.KeyboardType.NUMBER)
        self.auth_dropdown = ft.Dropdown(
            label="Autenticación",
            value=self.config.auth_mode,
            width=210,
            text_size=13,
            color=TEXT,
            bgcolor=SURFACE_ALT,
            border_color=BORDER,
            focused_border_color=ACCENT,
            border_radius=10,
            options=[
                ft.dropdown.Option(key="auto", text="Automático (token)"),
                ft.dropdown.Option(key="manual", text="Manual (contraseña)"),
                ft.dropdown.Option(key="none", text="Nulo (público)"),
            ],
            on_change=self._on_auth_change,
        )
        self.guest_dropdown = ft.Dropdown(
            label="Permisos de invitado",
            value="write" if self.config.guest_write else "read",
            width=210,
            text_size=13,
            color=TEXT,
            bgcolor=SURFACE_ALT,
            border_color=BORDER,
            focused_border_color=ACCENT,
            border_radius=10,
            options=[
                ft.dropdown.Option(key="read", text="Solo lectura"),
                ft.dropdown.Option(key="write", text="Control total"),
            ],
        )
        self.token_field = self._field("Token de acceso", self.config.auto_token or generate_auto_token(), read_only=True, expand=True)
        self.regenerate_button = ft.IconButton(
            icon=ft.icons.REFRESH,
            tooltip="Generar nuevo token",
            icon_color=ACCENT,
            on_click=self._regenerate_token,
        )
        self.password_field = self._field("Contraseña", password=True, can_reveal_password=True, expand=True)
        self.setup_error = ft.Text("", color=DANGER, size=12)
        self.auth_extra = ft.Column(spacing=8, controls=[])
        self._render_auth_extra()

        logo = ft.Row(
            [
                ft.Container(
                    width=44,
                    height=44,
                    border_radius=12,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=self._brand_image(44),
                ),
                ft.Column(
                    [
                        ft.Text("VORTEX", size=22, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text("Servidor NAS local · Cero fricción", size=11, color=MUTED),
                    ],
                    spacing=0,
                ),
                ft.Container(expand=True),
                ft.IconButton(
                    icon=ft.icons.PALETTE,
                    tooltip="Personalizar colores",
                    icon_color=ACCENT,
                    on_click=self._show_appearance_settings,
                ) if not self.is_desktop else ft.Container(),
            ],
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        boton_iniciar = ft.ElevatedButton(
            text="Iniciar servidor",
            icon=ft.icons.PLAY_ARROW,
            bgcolor=ACCENT_STRONG,
            color="#FFFFFF",
            height=46,
            width=200,
            style=ft.ButtonStyle(
                shape=ft.RoundedRectangleBorder(radius=10),
                elevation=0,
                padding=ft.padding.symmetric(horizontal=22, vertical=12),
            ),
            on_click=self._start_server,
        )

        formulario = ft.Column(
            [
                logo,
                ft.Divider(color=BORDER),
                ft.Row([self.dir_field, ft.IconButton(icon=ft.icons.FOLDER_OPEN, icon_color=ACCENT, tooltip="Elegir carpeta", on_click=self._pick_directory)]),
                ft.Row([self.port_field, self.auth_dropdown], spacing=16),
                ft.Row(
                    [
                        self.guest_dropdown,
                        ft.Text(
                            "Solo lectura impide crear, editar o borrar; Control total permite CRUD completo.",
                            size=11,
                            color=MUTED,
                            expand=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self.auth_extra,
                ft.Divider(color=BORDER),
                self.setup_error,
                ft.Row(
                    [
                        ft.Text(
                            "El servidor escucha en toda la LAN; ajusta aquí los permisos de los invitados.",
                            size=11,
                            color=MUTED,
                            expand=True,
                        ),
                        boton_iniciar,
                    ],
                    spacing=16,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Divider(color=BORDER),
                ft.Row(
                    [
                        ft.Icon(name=ft.icons.LAN, color=ACCENT, size=20),
                        ft.Column(
                            [
                                ft.Text("¿Ya hay un servidor Vortex en tu red?", color=TEXT, size=12, weight=ft.FontWeight.W_600),
                                ft.Text("Conéctate desde esta misma aplicación.", color=MUTED, size=11),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.OutlinedButton("Conectar", icon=ft.icons.LINK, on_click=self._show_connect_dialog),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=14,
        )

        contenido_pagina = ft.Container(
            expand=True,
            bgcolor=BG,
            alignment=ft.alignment.center,
            content=ft.Column(
                [self._card(formulario)],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._set_root(contenido_pagina)

    # Conexión a un servidor remoto descubierto o escrito a mano.
    def _show_connect_dialog(self, e: ft.ControlEvent | None = None) -> None:
        campo_host = self._field("IP o dirección del servidor", value="", hint_text="192.168.1.25 o http://vortex.local")
        campo_puerto = self._field("Puerto", value="8080", width=120, keyboard_type=ft.KeyboardType.NUMBER)
        texto_error = ft.Text("", color=DANGER, size=12)
        self.discovered_status = ft.Text("Buscando servidores Vortex en la red local…", color=MUTED, size=11)
        self.discovered_servers_control = ft.Column(
            height=135,
            spacing=5,
            scroll=ft.ScrollMode.AUTO,
        )
        self.discovered_render_signature = None

        def connect(ev: ft.ControlEvent) -> None:
            host_crudo = (campo_host.value or "").strip()
            try:
                puerto = int((campo_puerto.value or "8080").strip())
                if not 1 <= puerto <= 65535:
                    raise ValueError
            except ValueError:
                texto_error.value = "El puerto debe ser un número entre 1 y 65535"
                self.page.update()
                return
            if not host_crudo:
                texto_error.value = "Escribe la IP o el nombre del servidor"
                self.page.update()
                return
            try:
                url_remota = self._normalize_remote_url(host_crudo, puerto)
            except ValueError as excepcion:
                texto_error.value = str(excepcion)
                self.page.update()
                return
            self.page.close(dialogo)
            self._connect_remote(url_remota)

        campo_host.on_submit = connect
        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Conectar a un servidor Vortex", color=TEXT),
            content=ft.Container(
                width=500,
                content=ft.Column(
                    [
                        ft.Text("Servidores detectados automáticamente", color=TEXT, size=12, weight=ft.FontWeight.W_600),
                        ft.Row(
                            [
                                self.discovered_status,
                                ft.IconButton(
                                    icon=ft.icons.REFRESH,
                                    tooltip="Buscar de nuevo",
                                    icon_color=ACCENT,
                                    on_click=self._refresh_local_discovery,
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        ),
                        self.discovered_servers_control,
                        ft.Divider(color=BORDER, height=8),
                        ft.Text("O escribe la IP y el puerto manualmente:", color=MUTED, size=12),
                        campo_host,
                        campo_puerto,
                        texto_error,
                    ],
                    tight=True,
                    spacing=12,
                ),
            ),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialogo)),
                ft.ElevatedButton("Conectar", icon=ft.icons.LINK, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=connect),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.connect_dialog = dialogo
        self.page.open(dialogo)
        self._refresh_discovered_servers()

    def _refresh_local_discovery(self, e: ft.ControlEvent | None = None) -> None:
        self.discovery_started_at = time.monotonic()
        self.discovered_render_signature = None
        if self.discovery_browser is not None:
            try:
                self.discovery_browser.cancel()
                with self.discovery_lock:
                    self.discovered_servers.clear()
                    self.discovered_service_urls.clear()
                self.discovery_browser = ServiceBrowser(
                    self.discovery_zeroconf,
                    "_http._tcp.local.",
                    self.discovery_listener,
                )
            except Exception as excepcion:
                self.discovery_error = str(excepcion)
        self._refresh_discovered_servers()

    @staticmethod
    def _normalize_remote_url(value: str, default_port: int) -> str:
        crudo = value if "://" in value else f"http://{value}"
        analizado = urlsplit(crudo)
        if analizado.scheme not in {"http", "https"} or not analizado.hostname:
            raise ValueError("Usa una dirección http:// o https:// válida")
        if analizado.username or analizado.password:
            raise ValueError("No incluyas credenciales en la dirección del servidor")
        if analizado.path not in {"", "/"} or analizado.query or analizado.fragment:
            raise ValueError("Escribe solo la IP o el nombre del servidor, sin rutas adicionales")
        try:
            puerto_existente = analizado.port
        except ValueError as excepcion:
            raise ValueError("El puerto de la dirección no es válido") from excepcion
        dominio = analizado.netloc if puerto_existente is not None else f"{analizado.netloc}:{default_port}"
        return urlunsplit((analizado.scheme, dominio, "", "", ""))

    def _connect_remote(self, remote_url: str) -> None:
        cliente = httpx.Client(base_url=remote_url, timeout=httpx.Timeout(10.0, read=None))
        try:
            respuesta = cliente.get("/api/auth/status")
            respuesta.raise_for_status()
            estado_auth = respuesta.json()
        except (httpx.HTTPError, ValueError) as excepcion:
            cliente.close()
            self._notify(f"No se pudo conectar con {remote_url}: {excepcion}", True)
            return
        self.http = cliente
        self.base_url = remote_url
        self.file_revision = 0
        self.file_sync_checked_at = 0.0
        self.is_remote = True
        self.token = None
        self.role = None
        self.remote_auth_mode = str(estado_auth.get("auth_mode", "none"))
        self.remote_requires_login = bool(estado_auth.get("requires_login"))
        self.remote_guest_write = bool(estado_auth.get("guest_write", True))
        self._start_polling()
        if self.remote_requires_login:
            self.show_login()
            return
        try:
            respuesta = cliente.get("/api/auth/me")
            respuesta.raise_for_status()
            self.role = respuesta.json().get("role", "guest_read")
        except (httpx.HTTPError, ValueError) as excepcion:
            self._disconnect_remote(None)
            self._notify(f"El servidor respondió, pero no permitió el acceso: {excepcion}", True)
            return
        self.current_path = ""
        self.show_browser()

    def _disconnect_remote(self, e: ft.ControlEvent | None) -> None:
        if self.http is not None:
            self.http.close()
        self.http = None
        self.base_url = ""
        self.token = None
        self.role = None
        self.is_remote = False
        self.current_path = ""
        self.show_setup()

    def _cancel_remote_login(self, e: ft.ControlEvent | None) -> None:
        if self.remote_requires_login:
            self._disconnect_remote(e)
            return
        self.show_browser()

    def _render_auth_extra(self) -> None:
        modo = self.auth_dropdown.value
        self.auth_extra.controls.clear()
        if modo == "auto":
            self.auth_extra.controls.append(
                ft.Row([self.token_field, self.regenerate_button], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)
            )
        elif modo == "manual":
            self.auth_extra.controls.append(self.password_field)
        else:
            self.auth_extra.controls.append(
                ft.Container(
                    padding=10,
                    border_radius=8,
                    bgcolor=SURFACE_ALT,
                    border=ft.border.all(1, WARNING),
                    content=ft.Text("Acceso público: cualquier dispositivo de la LAN podrá entrar sin credenciales.", size=12, color=WARNING),
                )
            )
        try:
            self.page.update()
        except Exception:
            pass

    def _on_auth_change(self, e: ft.ControlEvent) -> None:
        self._render_auth_extra()

    def _regenerate_token(self, e: ft.ControlEvent) -> None:
        self.token_field.value = generate_auto_token()
        self.page.update()

    def _pick_directory(self, e: ft.ControlEvent) -> None:
        if sys.platform.startswith("linux") or bool(getattr(self.page, "web", False)):
            self._show_folder_picker()
            return
        self.dir_picker.get_directory_path(dialog_title="Seleccionar carpeta raíz")

    def _show_folder_picker(self) -> None:
        actual = Path(self.dir_field.value or Path.home()).expanduser()
        if not actual.is_dir():
            actual = Path.home()
        self._show_local_picker(actual, "folder")

    def _show_file_picker(self) -> None:
        actual = Path(self.config.base_dir or Path.home()).expanduser()
        if self.current_path:
            actual = actual / self.current_path
        if not actual.is_dir():
            actual = Path.home()
        self._show_local_picker(actual, "files")

    def _show_local_picker(self, current: Path, mode: str) -> None:
        self.folder_picker_path = current.resolve()
        self.folder_picker_mode = mode
        self.folder_picker_selected.clear()
        self.folder_picker_path_text = ft.Text("", size=12, color=TEXT, selectable=True, font_family="monospace")
        self.folder_picker_selection_text = ft.Text("", size=11, color=MUTED)
        self.folder_picker_list = ft.ListView(expand=True, spacing=2, controls=[])
        modo_carpeta = mode == "folder"
        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Seleccionar carpeta raíz" if modo_carpeta else "Seleccionar archivos", color=TEXT),
            content=ft.Container(
                width=600,
                height=460,
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.IconButton(icon=ft.icons.ARROW_UPWARD, tooltip="Carpeta superior", on_click=self._folder_picker_up),
                                ft.Container(expand=True, content=self.folder_picker_path_text),
                            ],
                            spacing=8,
                        ),
                        self.folder_picker_selection_text,
                        ft.Divider(color=BORDER),
                        self.folder_picker_list,
                    ],
                    spacing=6,
                ),
            ),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialogo)),
                ft.ElevatedButton(
                    "Usar esta carpeta" if modo_carpeta else "Subir archivos",
                    icon=ft.icons.CHECK if modo_carpeta else ft.icons.UPLOAD_FILE,
                    bgcolor=ACCENT_STRONG,
                    color="#FFFFFF",
                    on_click=lambda ev: self._use_folder_picker_path(dialogo) if modo_carpeta else self._use_file_picker_selection(dialogo),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.folder_picker_dialog = dialogo
        self._render_folder_picker()
        self.page.open(dialogo)

    def _render_folder_picker(self) -> None:
        if self.folder_picker_list is None or self.folder_picker_path_text is None or self.folder_picker_selection_text is None:
            return
        self.folder_picker_path_text.value = str(self.folder_picker_path)
        self.folder_picker_selection_text.value = (
            f"{len(self.folder_picker_selected)} archivo(s) seleccionado(s)"
            if self.folder_picker_mode == "files"
            else ""
        )
        self.folder_picker_list.controls.clear()
        try:
            entradas = list(self.folder_picker_path.iterdir())
            carpetas = sorted((entrada for entrada in entradas if entrada.is_dir()), key=lambda entrada: entrada.name.casefold())
            archivos = sorted((entrada for entrada in entradas if entrada.is_file()), key=lambda entrada: entrada.name.casefold())
        except OSError as excepcion:
            self.folder_picker_list.controls.append(ft.Text(f"No se pudo leer esta carpeta: {excepcion}", color=DANGER, size=12))
            self.page.update()
            return
        if not carpetas and self.folder_picker_mode == "folder":
            self.folder_picker_list.controls.append(ft.Text("No hay subcarpetas aquí.", color=MUTED, size=12))
        for carpeta in carpetas:
            self.folder_picker_list.controls.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=8, vertical=6),
                    border_radius=8,
                    on_click=lambda ev, target=carpeta: self._open_folder_picker_path(target),
                    content=ft.Row(
                        [
                            ft.Icon(name=ft.icons.FOLDER, color=ACCENT, size=19),
                            ft.Text(carpeta.name, color=TEXT, size=13, expand=True, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Icon(name=ft.icons.CHEVRON_RIGHT, color=MUTED, size=18),
                        ],
                        spacing=8,
                    ),
                )
            )
        if self.folder_picker_mode == "files":
            for ruta in archivos:
                tipo = file_kind(ruta.name, False)
                self.folder_picker_list.controls.append(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        border_radius=8,
                        content=ft.Row(
                            [
                                ft.Icon(name=icon_for_kind(tipo), color=color_for_kind(tipo), size=18),
                                ft.Checkbox(
                                    label=ruta.name,
                                    value=ruta in self.folder_picker_selected,
                                    expand=True,
                                    on_change=lambda ev, target=ruta: self._toggle_file_picker_selection(target, bool(ev.control.value)),
                                ),
                            ],
                            spacing=8,
                        ),
                    )
                )
            if not carpetas and not archivos:
                self.folder_picker_list.controls.append(ft.Text("Esta carpeta está vacía.", color=MUTED, size=12))
        self.page.update()

    def _toggle_file_picker_selection(self, path: Path, selected: bool) -> None:
        if selected:
            self.folder_picker_selected.add(path)
        else:
            self.folder_picker_selected.discard(path)
        self._render_folder_picker()

    def _open_folder_picker_path(self, path: Path) -> None:
        self.folder_picker_path = path
        self._render_folder_picker()

    def _folder_picker_up(self, e: ft.ControlEvent) -> None:
        padre = self.folder_picker_path.parent
        if padre != self.folder_picker_path:
            self.folder_picker_path = padre
            self._render_folder_picker()

    def _use_folder_picker_path(self, dialog: ft.AlertDialog) -> None:
        self.dir_field.value = str(self.folder_picker_path)
        self.page.close(dialog)
        self.page.update()

    def _use_file_picker_selection(self, dialog: ft.AlertDialog) -> None:
        if not self.folder_picker_selected:
            self._notify("Selecciona uno o más archivos para subir", True)
            return
        rutas = [str(ruta) for ruta in sorted(self.folder_picker_selected, key=lambda item: item.name.casefold())]
        self.page.close(dialog)
        threading.Thread(target=self._upload_worker, args=(rutas,), daemon=True).start()

    def _on_dir_picked(self, e: ft.FilePickerResultEvent) -> None:
        if e.path:
            self.dir_field.value = e.path
            self.page.update()

    # Arranque del servidor local con la configuración validada.
    def _start_server(self, e: ft.ControlEvent) -> None:
        raiz = (self.dir_field.value or "").strip()
        if not raiz:
            self._notify("Selecciona una carpeta raíz", True)
            return
        try:
            puerto = int((self.port_field.value or "").strip())
        except ValueError:
            self._notify("El puerto debe ser un número entero entre 1 y 65535", True)
            return
        configuracion = ServerConfig(
            base_dir=raiz,
            port=puerto,
            auth_mode=self.auth_dropdown.value,
            auto_token=(self.token_field.value or "").strip() or generate_auto_token(),
            guest_write=(self.guest_dropdown.value or "write") == "write",
            password_hash=self.config.password_hash,
            hostname=self.config.hostname,
            chunk_size=self.config.chunk_size,
            direct_upload_limit=self.config.direct_upload_limit,
        )
        if configuracion.auth_mode == "manual":
            contrasena = self.password_field.value or ""
            if contrasena:
                configuracion.manual_password = contrasena
            elif self.config.password_hash:
                configuracion.password_hash = self.config.password_hash
            else:
                self.setup_error.value = "El modo manual requiere una contraseña"
                self.page.update()
                return
        try:
            configuracion.validate()
            validate_port(configuracion.port)
        except (TypeError, ValueError) as excepcion:
            sugerencia = suggest_free_port(configuracion.port + 1) if "uso" in str(excepcion) else None
            texto = str(excepcion)
            if sugerencia:
                texto = f"{texto}. Sugerencia: {sugerencia}"
            self.setup_error.value = texto
            self._notify(texto, True)
            return
        ruta_base = Path(configuracion.base_dir).expanduser()
        if needs_elevation(ruta_base):
            self._confirm_elevation(configuracion, ruta_base)
            return
        self._launch(configuracion)

    def _confirm_elevation(self, cfg: ServerConfig, base_path: Path) -> None:
        def do_elevate(e: ft.ControlEvent) -> None:
            self.page.close(dialogo)
            if cfg.auth_mode == "manual" and cfg.manual_password and not cfg.password_hash:
                from ..backend.auth import AuthManager

                cfg.password_hash = AuthManager.hash_password(cfg.manual_password)
            try:
                self.store.save(cfg)
            except Exception:
                pass
            try:
                log_standalone(
                    action="PRIVILEGE_REQUEST",
                    severity=AUDIT_WARNING,
                    target_path=str(base_path),
                    status_code=200,
                    details={"requested": True, "granted": None, "platform": sys.platform},
                )
            except Exception:
                pass
            if relaunch_elevated([sys.executable, *sys.argv]):
                sys.exit(0)
            try:
                log_standalone(
                    action="PRIVILEGE_REQUEST",
                    severity=AUDIT_WARNING,
                    target_path=str(base_path),
                    status_code=403,
                    details={"requested": True, "granted": False, "platform": sys.platform},
                )
            except Exception:
                pass
            self._notify("No se pudo solicitar elevación de privilegios. Ejecuta Vortex como administrador.", True)

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Se requieren permisos de administrador", color=TEXT),
            content=ft.Text(
                f"La carpeta seleccionada requiere privilegios elevados:\n{base_path}\n\n"
                "Si deniegas la elevación, Vortex no se cerrará: podrás elegir otra carpeta.",
                color=MUTED,
                size=13,
            ),
            actions=[
                ft.TextButton("Elegir otra carpeta", on_click=lambda e: self.page.close(dialogo)),
                ft.ElevatedButton("Solicitar elevación", bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=do_elevate),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    def _launch(self, cfg: ServerConfig) -> None:
        try:
            entorno = self.server.start(cfg)
        except (ServerStartError, ValueError) as excepcion:
            self._notify(str(excepcion), True)
            return
        except Exception as excepcion:
            self._notify(f"No se pudo iniciar el servidor ({type(excepcion).__name__}): {excepcion}", True)
            return
        self.config = cfg
        self.is_remote = False
        try:
            self.store.save(cfg)
        except Exception:
            pass
        self.base_url = f"http://127.0.0.1:{cfg.port}"
        self.http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(120.0, read=None))
        self.file_revision = 0
        self.file_sync_checked_at = 0.0
        self.log_buffer.clear()
        self.log_unsubscribe = entorno.audit.add_listener(self._audit_listener)
        self._start_polling()
        self._enter_app()

    # Sincronización periódica de cambios y estado del servidor.
    def _start_polling(self) -> None:
        if self.poll_started:
            return
        self.poll_started = True
        self.page.run_task(self._poll_loop)

    def _audit_listener(self, record: dict) -> None:
        self.log_buffer.append(record)

    async def _poll_loop(self) -> None:
        while True:
            try:
                self._refresh_discovered_servers()
                self._refresh_upload_widget()
                await self._sync_file_changes()
                if self.remote_download_notice is not None:
                    mensaje, es_error = self.remote_download_notice
                    self.remote_download_notice = None
                    self._notify(mensaje, es_error)
                if self.logs_dialog_open:
                    self._refresh_logs()
            except Exception:
                pass
            await asyncio.sleep(0.4)

    async def _sync_file_changes(self) -> None:
        if self.http is None or time.monotonic() - self.file_sync_checked_at < 1.0:
            return
        self.file_sync_checked_at = time.monotonic()
        try:
            respuesta = await asyncio.to_thread(
                self.http.get,
                "/api/system/changes",
                params={"since": self.file_revision},
                headers=self._auth_headers(),
                timeout=2.5,
            )
        except httpx.HTTPError:
            return
        if respuesta.status_code != 200:
            return
        try:
            datos = respuesta.json()
        except ValueError:
            return
        cambios = datos.get("changes") or []
        self.file_revision = int(datos.get("revision", self.file_revision))
        if not cambios:
            return
        ruta = self.current_path
        refrescar = False
        for cambio in cambios:
            eliminados = cambio.get("removed")
            if eliminados and (ruta == eliminados or ruta.startswith(eliminados + "/")):
                ruta = eliminados.rpartition("/")[0]
                refrescar = True
            renombrado = cambio.get("renamed")
            if renombrado:
                anterior = renombrado.get("from", "")
                if anterior and (ruta == anterior or ruta.startswith(anterior + "/")):
                    ruta = renombrado.get("to", "") + ruta[len(anterior):]
                    refrescar = True
            if ruta in (cambio.get("paths") or []):
                refrescar = True
        if refrescar:
            self.refresh_listing(ruta)

    # Sesión local: login automático o pantalla de acceso.
    def _enter_app(self) -> None:
        if self._auto_login():
            self.show_browser()
            return
        self.show_login()

    def _auto_login(self) -> bool:
        if self.config.auth_mode == "none":
            try:
                respuesta = httpx.get(f"{self.base_url}/api/auth/me", timeout=10)
            except httpx.HTTPError:
                return False
            if respuesta.status_code == 200:
                self.role = respuesta.json().get("role")
                self.token = None
                return True
            return False
        secreto = None
        if self.config.auth_mode == "auto":
            secreto = self.config.auto_token
        elif self.config.auth_mode == "manual" and self.config.manual_password:
            secreto = self.config.manual_password
        if not secreto:
            return False
        return self._post_login(secreto)

    def _post_login(self, secret: str) -> bool:
        if self.http is None:
            return False
        try:
            respuesta = self.http.post("/api/auth/login", json={"secret": secret}, timeout=20)
        except httpx.HTTPError:
            return False
        if respuesta.status_code != 200:
            return False
        datos = respuesta.json()
        self.token = datos.get("access_token")
        self.role = datos.get("role")
        if self.http is not None:
            self.http.headers["Authorization"] = f"Bearer {self.token}"
        return True

    # Pantalla de acceso con contraseña o entrada como invitado.
    def show_login(self, error: str = "") -> None:
        etiqueta_credencial = "Token o contraseña del servidor" if self.is_remote else "Contraseña de administrador"
        contrasena = self._field(etiqueta_credencial, password=True, can_reveal_password=True, autofocus=True)
        texto_error = ft.Text(error, color=DANGER, size=12)
        ocupado = ft.ProgressRing(width=18, height=18, visible=False, color=ACCENT)

        def attempt(e: ft.ControlEvent) -> None:
            if not contrasena.value:
                texto_error.value = "Introduce la contraseña"
                self.page.update()
                return
            ocupado.visible = True
            texto_error.value = ""
            self.page.update()
            correcto = self._post_login(contrasena.value)
            ocupado.visible = False
            if correcto:
                self.show_browser()
            else:
                texto_error.value = "Credenciales inválidas o bloqueo temporal por intentos fallidos"
                self.page.update()

        def continue_as_guest(e: ft.ControlEvent | None = None) -> None:
            self.token = None
            escritura_invitado = self.remote_guest_write if self.is_remote else self.config.guest_write
            self.role = "guest_write" if escritura_invitado else "guest_read"
            if self.http is not None:
                self.http.headers.pop("Authorization", None)
            self.show_browser()

        contrasena.on_submit = attempt
        accion_secundaria = (
            ft.TextButton("Cancelar", on_click=self._cancel_remote_login)
            if self.is_remote and self.remote_requires_login
            else ft.OutlinedButton("Continuar como invitado", on_click=continue_as_guest)
        )
        tarjeta = self._card(
            ft.Column(
                [
                    ft.Container(
                        width=56,
                        height=56,
                        border_radius=16,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        content=self._brand_image(56),
                    ),
                    ft.Text("Acceso a Vortex", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(
                        "Introduce el token o la contraseña configurados en ese servidor."
                        if self.is_remote
                        else "Introduce la contraseña configurada en el servidor.",
                        size=12,
                        color=MUTED,
                    ),
                    contrasena,
                    texto_error,
                    ft.Row(
                        [
                            accion_secundaria,
                            ocupado,
                            ft.Container(expand=True),
                            ft.ElevatedButton("Entrar", icon=ft.icons.LOGIN, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=attempt),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=14,
            ),
            width=440,
        )
        self._set_root(
            ft.Container(
                expand=True,
                bgcolor=BG,
                alignment=ft.alignment.center,
                content=ft.Column([tarjeta], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            )
        )

    def _set_root(self, control: ft.Control) -> None:
        self.page.controls.clear()
        self.page.add(self._window_shell(control))
        self.page.update()

    def _auth_headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    # Peticiones HTTP comunes con cabeceras de autenticación.
    def _request(self, method: str, url: str, **kwargs):
        if self.http is None:
            return None
        cabeceras = dict(self._auth_headers())
        cabeceras.update(kwargs.pop("headers", {}) or {})
        try:
            respuesta = self.http.request(method, url, headers=cabeceras, **kwargs)
        except httpx.HTTPError as excepcion:
            self._notify(f"Error de conexión: {excepcion}", True)
            return None
        if respuesta.status_code == 401:
            self.token = None
            self.show_login("La sesión expiró. Vuelve a iniciar sesión.")
            return None
        if respuesta.status_code >= 400:
            detalle = respuesta.text
            try:
                carga = respuesta.json()
                detalle = carga.get("detail", detalle)
                if isinstance(detalle, dict):
                    detalle = detalle.get("message", str(detalle))
            except Exception:
                pass
            self._notify(str(detalle), True)
            return None
        try:
            return respuesta.json()
        except ValueError:
            return None

    # Navegador de archivos: cabecera, listado y acciones.
    def show_browser(self) -> None:
        self.search_field = ft.TextField(
            hint_text="Buscar o comando…   Ctrl+K",
            prefix_icon=ft.icons.SEARCH,
            width=430,
            height=44,
            text_size=13,
            color=TEXT,
            hint_style=ft.TextStyle(color=MUTED, size=12),
            bgcolor=SURFACE_ALT,
            border_color=BORDER,
            focused_border_color=ACCENT,
            border_radius=10,
            content_padding=ft.padding.symmetric(horizontal=12, vertical=10),
            on_submit=self._on_search_submit,
        )
        self.breadcrumb = ft.Row(spacing=2, scroll=ft.ScrollMode.AUTO, expand=True)
        self.count_text = ft.Text("", size=11, color=MUTED)
        self.listing = ft.ListView(expand=True, spacing=2, padding=ft.padding.symmetric(vertical=10), controls=[])

        color_insignia = SUCCESS if self.role == "host" else ACCENT
        etiqueta_rol = {"host": "HOST", "guest_write": "INVITADO · TOTAL", "guest_read": "INVITADO · LECTURA"}.get(self.role or "", "INVITADO")
        if self.is_remote and self.role == "host":
            etiqueta_rol = "HOST REMOTO"
        acciones_cabecera: list[ft.Control] = []
        if not self.is_desktop:
            acciones_cabecera.append(
                ft.IconButton(
                    icon=ft.icons.PALETTE,
                    icon_color=ACCENT,
                    tooltip="Personalizar colores",
                    on_click=self._show_appearance_settings,
                )
            )
        if self.is_remote:
            if self.remote_auth_mode != "none" and self.role != "host":
                acciones_cabecera.append(
                    ft.IconButton(
                        icon=ft.icons.LOGIN,
                        icon_color=ACCENT,
                        tooltip="Iniciar sesión como anfitrión",
                        on_click=lambda ev: self.show_login(),
                    )
                )
            acciones_cabecera.append(
                ft.IconButton(
                    icon=ft.icons.LINK_OFF,
                    icon_color=DANGER,
                    tooltip="Desconectar del servidor",
                    on_click=self._disconnect_remote,
                )
            )
        else:
            if self.config.auth_mode != "none" and self.role != "host":
                acciones_cabecera.append(
                    ft.IconButton(
                        icon=ft.icons.LOGIN,
                        icon_color=ACCENT,
                        tooltip="Iniciar sesión como anfitrión",
                        on_click=lambda e: self.show_login(),
                    )
                )
            acciones_cabecera.extend(
                [
                    ft.TextButton(text="Acceso LAN", icon=ft.icons.WIFI_TETHERING, tooltip="Cómo entrar desde la red local", on_click=self._show_lan_help),
                    ft.IconButton(icon=ft.icons.TERMINAL, icon_color=MUTED, tooltip="Consola de logs (host)", on_click=self._open_logs),
                    ft.IconButton(icon=ft.icons.POWER_SETTINGS_NEW, icon_color=DANGER, tooltip="Detener servidor", on_click=self._stop_server),
                ]
            )
        cabecera = ft.Container(
            bgcolor=SURFACE,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            padding=ft.padding.symmetric(horizontal=22, vertical=12),
            content=ft.Row(
                [
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        border_radius=6,
                        bgcolor=SURFACE_ALT,
                        border=ft.border.all(1, BORDER),
                        content=ft.Row(
                            [
                                ft.Container(width=7, height=7, border_radius=4, bgcolor=color_insignia),
                                ft.Text(etiqueta_rol, size=10, color=color_insignia, weight=ft.FontWeight.W_600),
                            ],
                            spacing=5,
                        ),
                    ),
                    ft.Container(expand=True),
                    self.search_field,
                    *acciones_cabecera,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        barra_herramientas = ft.Container(
            padding=ft.padding.symmetric(horizontal=22, vertical=8),
            content=ft.Row(
                [
                    ft.IconButton(icon=ft.icons.ARROW_UPWARD, icon_color=MUTED, tooltip="Subir", on_click=self._go_up),
                    ft.IconButton(icon=ft.icons.REFRESH, icon_color=MUTED, tooltip="Actualizar", on_click=lambda e: self.refresh_listing(self.current_path)),
                    ft.IconButton(icon=ft.icons.CREATE_NEW_FOLDER_OUTLINED, icon_color=ACCENT, tooltip="Nueva carpeta", on_click=self._new_folder),
                    ft.IconButton(icon=ft.icons.NOTE_ADD_OUTLINED, icon_color=ACCENT, tooltip="Escribir texto y crear .txt", on_click=self._new_text_file),
                    ft.IconButton(icon=ft.icons.UPLOAD_FILE, icon_color=ACCENT, tooltip="Subir archivos", on_click=self._pick_upload),
                    ft.IconButton(icon=ft.icons.ARCHIVE, icon_color=ACCENT, tooltip="Descargar carpeta (.zip)", on_click=self._download_zip),
                    ft.VerticalDivider(width=1, color=BORDER),
                    self.breadcrumb,
                    self.count_text,
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        self.upload_bar = ft.ProgressBar(value=0, color=ACCENT, bgcolor=SURFACE_ALT)
        self.upload_name_text = ft.Text("", size=12, color=TEXT, weight=ft.FontWeight.W_600)
        self.upload_speed_text = ft.Text("", size=11, color=MUTED)
        self.upload_cancel_button = ft.IconButton(
            icon=ft.icons.CLOSE,
            tooltip="Cancelar subida por bloques",
            icon_color=DANGER,
            on_click=self._cancel_upload,
            icon_size=16,
            visible=False,
        )
        self.upload_widget = ft.Container(
            visible=False,
            right=24,
            bottom=24,
            width=340,
            bgcolor=SURFACE,
            border=ft.border.all(1, BORDER),
            border_radius=14,
            padding=16,
            shadow=ft.BoxShadow(blur_radius=24, color="#66000000", offset=ft.Offset(0, 8)),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(name=ft.icons.CLOUD_UPLOAD, color=ACCENT, size=20),
                            ft.Text("Mini-Vortex", size=12, weight=ft.FontWeight.BOLD, color=ACCENT),
                            ft.Container(expand=True),
                            self.upload_speed_text,
                            self.upload_cancel_button,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.upload_name_text,
                    self.upload_bar,
                ],
                spacing=8,
            ),
        )

        cuerpo = ft.Container(expand=True, padding=ft.padding.symmetric(horizontal=18), content=self.listing)
        columna = ft.Column([cabecera, barra_herramientas, cuerpo], expand=True, spacing=0)
        pila = ft.Stack([ft.Container(expand=True, content=columna), self.upload_widget], expand=True)
        self._set_root(pila)
        self.refresh_listing(self.current_path)

    def _show_lan_help(self, e: ft.ControlEvent | None = None) -> None:
        puerto = self.config.port
        nombre_host = self.config.hostname.rstrip(".")
        direccion = local_ip()
        url_ip = f"http://{direccion}:{puerto}"
        url_nombrada = f"http://{nombre_host}:{puerto}"
        imagen_qr = qrcode.make(url_ip)
        buffer_qr = io.BytesIO()
        imagen_qr.save(buffer_qr)
        qr_base64 = base64.b64encode(buffer_qr.getvalue()).decode("ascii")
        nota_firewall = (
            ft.Text(
                f"En Linux con UFW, permite TCP {puerto} desde tu red local. "
                f"Abrir 8080 no abre otros puertos. "
                f"Ejemplo: sudo ufw allow from <SUBRED_LOCAL> "
                f"to any port {puerto} proto tcp",
                color=MUTED,
                size=12,
                selectable=True,
            )
            if sys.platform.startswith("linux")
            else ft.Container()
        )
        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Icon(name=ft.icons.WIFI_TETHERING, color=ACCENT),
                    ft.Text("Encontrar Vortex en la red local", color=TEXT, size=16),
                ],
                spacing=8,
            ),
            content=ft.Container(
                width=560,
                content=ft.Column(
                    [
                        ft.Text("En otro equipo conectado a la misma red, abre esta dirección IP:", color=MUTED, size=13),
                        ft.Row(
                            [
                                ft.Container(
                                    padding=8,
                                    border_radius=10,
                                    bgcolor="#FFFFFF",
                                    content=ft.Image(
                                        src_base64=qr_base64,
                                        width=176,
                                        height=176,
                                        fit=ft.ImageFit.CONTAIN,
                                    ),
                                ),
                                ft.Container(
                                    expand=True,
                                    padding=12,
                                    border_radius=10,
                                    bgcolor=SURFACE_ALT,
                                    border=ft.border.all(1, BORDER),
                                    content=ft.Column(
                                        [
                                            ft.Text("Escanea para conectarte", color=ACCENT, size=14, weight=ft.FontWeight.W_600),
                                            ft.Text("IP del entorno donde corre Vortex", color=MUTED, size=11),
                                            ft.Text(url_ip, color=TEXT, size=14, selectable=True, font_family="monospace"),
                                            ft.Divider(color=BORDER, height=8),
                                            ft.Text("Nombre mDNS (puede no funcionar en ChromeOS)", color=MUTED, size=11),
                                            ft.Text(url_nombrada, color=MUTED, size=12, selectable=True, font_family="monospace"),
                                        ],
                                        spacing=6,
                                    ),
                                ),
                            ],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Text(
                            "Los invitados tienen control total sin iniciar sesión. Si Vortex corre en el Linux "
                            "de un Chromebook, esta IP puede ser solo del contenedor: habilita el reenvío "
                            f"TCP del puerto {puerto} en Configuración > Desarrolladores > Linux > Reenvío "
                            "de puertos y, desde el otro dispositivo, usa la IP Wi-Fi del Chromebook.",
                            color=MUTED,
                            size=12,
                        ),
                        nota_firewall,
                    ],
                    tight=True,
                    spacing=14,
                ),
            ),
            actions=[
                ft.TextButton("Cerrar", on_click=lambda ev: self.page.close(dialogo)),
                ft.ElevatedButton(
                    "Abrir IP aquí",
                    icon=ft.icons.OPEN_IN_BROWSER,
                    bgcolor=ACCENT_STRONG,
                    color="#FFFFFF",
                    on_click=lambda ev: (self.page.close(dialogo), self._open_url(url_ip)),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    def _on_search_submit(self, e: ft.ControlEvent) -> None:
        consulta = (self.search_field.value or "").strip()
        if not consulta:
            self.refresh_listing(self.current_path)
            return
        datos = self._request("GET", "/api/files/search", params={"path": self.current_path, "q": consulta, "limit": 300})
        if datos is None:
            return
        self._render_items(datos["items"])
        sufijo = " · resultados truncados" if datos.get("truncated") else ""
        self.count_text.value = f"{datos['total']} coincidencias{sufijo}"
        self.page.update()

    def refresh_listing(self, path: str) -> None:
        datos = self._request("GET", "/api/files", params={"path": path})
        if datos is None:
            return
        self.current_path = datos["path"]
        self.parent_path = datos["parent"]
        self._render_items(datos["items"])
        self.count_text.value = f"{datos['total']} elementos"
        self._build_breadcrumb(self.current_path)
        self.page.update()

    def _build_breadcrumb(self, path: str) -> None:
        self.breadcrumb.controls.clear()
        boton_raiz = ft.TextButton(
            text="Raíz",
            icon=ft.icons.HOME_ROUNDED,
            on_click=lambda e: self.refresh_listing(""),
        )
        self.breadcrumb.controls.append(boton_raiz)
        if not path:
            return
        acumulado = []
        for parte in path.split("/"):
            acumulado.append(parte)
            objetivo = "/".join(acumulado)
            self.breadcrumb.controls.append(ft.Text("/", size=12, color=MUTED))
            self.breadcrumb.controls.append(
                ft.TextButton(text=parte, on_click=lambda e, t=objetivo: self.refresh_listing(t))
            )

    def _render_items(self, items: list[dict]) -> None:
        if self.listing is None:
            return
        self.listing.controls.clear()
        if not items:
            self.listing.controls.append(
                ft.Container(
                    padding=40,
                    alignment=ft.alignment.center,
                    content=ft.Column(
                        [
                            ft.Icon(name=ft.icons.SEARCH_OFF, color=BORDER, size=48),
                            ft.Text("Sin resultados", size=14, color=MUTED),
                            ft.Text("Prueba con otro término o usa operadores type:, ext:, size:", size=11, color=BORDER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=6,
                    ),
                )
            )
            return
        for elemento in items:
            self.listing.controls.append(self._build_row(elemento))

    def _build_row(self, item: dict) -> ft.Container:
        tipo = item["kind"]
        subtitulo = "Carpeta" if item["is_dir"] else human_size(item["size"])
        subtitulo += f"  ·  {format_time(item['modified'])}"
        acciones = []
        if item["is_dir"]:
            acciones.append(ft.IconButton(icon=ft.icons.ARCHIVE, icon_size=18, icon_color=MUTED, tooltip="Descargar .zip", on_click=lambda e, it=item: self._open_url(self._zip_url(it["path"]))))
        else:
            acciones.append(ft.IconButton(icon=ft.icons.DOWNLOAD, icon_size=18, icon_color=MUTED, tooltip="Descargar", on_click=lambda e, it=item: self._open_url(self._file_url(it["path"]))))
        if item.get("editable") and not item["is_dir"]:
            acciones.append(ft.IconButton(icon=ft.icons.EDIT_OUTLINED, icon_size=18, icon_color=ACCENT, tooltip="Editar", on_click=lambda e, it=item: self.show_editor(it)))
        acciones.append(ft.IconButton(icon=ft.icons.EDIT_NOTE, icon_size=18, icon_color=MUTED, tooltip="Renombrar", on_click=lambda e, it=item: self.show_rename(it)))
        acciones.append(ft.IconButton(icon=ft.icons.DELETE_OUTLINE, icon_size=18, icon_color=DANGER, tooltip="Eliminar", on_click=lambda e, it=item: self.confirm_delete(it)))
        icono = ft.Container(
            width=38,
            height=38,
            border_radius=10,
            bgcolor=SURFACE,
            alignment=ft.alignment.center,
            content=ft.Icon(name=icon_for_kind(tipo), color=color_for_kind(tipo), size=20),
        )
        detalles = ft.Column(
            [
                ft.Text(item["name"], size=14, color=TEXT, weight=ft.FontWeight.W_500, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                ft.Text(subtitulo, size=11, color=MUTED),
            ],
            spacing=1,
            expand=True,
        )
        objetivo_apertura = lambda e, it=item: self.open_item(it)
        principal = ft.Container(
            expand=True,
            on_click=objetivo_apertura,
            content=ft.Row([icono, detalles], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )
        fila = ft.Row(
            [
                principal,
                ft.Row(acciones, spacing=0),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=0,
        )
        return ft.Container(
            content=fila,
            padding=ft.padding.symmetric(horizontal=10, vertical=7),
            border_radius=10,
            on_hover=self._hover_row,
        )

    def _hover_row(self, e: ft.ControlEvent) -> None:
        try:
            e.control.bgcolor = SURFACE if e.data == "true" else None
            e.control.update()
        except Exception:
            pass

    def open_item(self, item: dict) -> None:
        if item["is_dir"]:
            self.refresh_listing(item["path"])
        else:
            self.show_preview(item)

    def _go_up(self, e: ft.ControlEvent) -> None:
        if self.parent_path is not None:
            self.refresh_listing(self.parent_path)

    def _file_url(self, path: str, inline: bool = False) -> str:
        parametros = {"path": path}
        if inline:
            parametros["inline"] = "1"
        if self.token:
            parametros["token"] = self.token
        return f"{self.base_url}/api/files/download?{urlencode(parametros)}"

    def _zip_url(self, path: str) -> str:
        parametros = {"path": path}
        if self.token:
            parametros["token"] = self.token
        return f"{self.base_url}/api/files/zip?{urlencode(parametros)}"

    def _open_url(self, url: str) -> None:
        if self.is_remote:
            threading.Thread(target=self._download_remote_url, args=(url,), daemon=True).start()
            return
        try:
            webbrowser.open(url)
        except Exception as excepcion:
            self._notify(f"No se pudo abrir el navegador: {excepcion}", True)

    def _download_remote_url(self, url: str) -> None:
        if self.http is None:
            self.remote_download_notice = ("La conexión con el servidor se cerró", True)
            return
        try:
            with self.http.stream("GET", url, headers=self._auth_headers(), timeout=None) as respuesta:
                respuesta.raise_for_status()
                disposicion = Message()
                if respuesta.headers.get("content-disposition"):
                    disposicion["content-disposition"] = respuesta.headers["content-disposition"]
                nombre_archivo = disposicion.get_filename()
                if not nombre_archivo:
                    consulta = parse_qs(urlsplit(url).query)
                    ruta_archivo = consulta.get("path", [""])[0]
                    nombre_archivo = Path(unquote(ruta_archivo)).name or "descarga"
                    if "/zip" in url and not nombre_archivo.endswith(".zip"):
                        nombre_archivo += ".zip"
                nombre_archivo = Path(nombre_archivo).name.replace("\x00", "") or "descarga"
                directorio_descarga = Path.home() / "Downloads"
                directorio_descarga.mkdir(parents=True, exist_ok=True)
                destino = directorio_descarga / nombre_archivo
                indice = 1
                while destino.exists():
                    destino = directorio_descarga / f"{Path(nombre_archivo).stem}_{indice}{Path(nombre_archivo).suffix}"
                    indice += 1
                temporal = destino.with_name(destino.name + ".part")
                with temporal.open("wb") as salida:
                    for bloque in respuesta.iter_bytes():
                        salida.write(bloque)
                temporal.replace(destino)
            self.remote_download_notice = (f"Guardado en {destino}", False)
        except Exception as excepcion:
            self.remote_download_notice = (f"No se pudo descargar el archivo: {excepcion}", True)

    # Vista previa de imágenes, texto, audio y vídeo.
    def show_preview(self, item: dict) -> None:
        tipo = item["kind"]
        contenido: ft.Control
        if tipo == "image":
            contenido = ft.Container(
                height=460,
                alignment=ft.alignment.center,
                content=ft.Image(src=self._file_url(item["path"], inline=True), fit=ft.ImageFit.CONTAIN, border_radius=8),
            )
        elif tipo in {"video", "audio"}:
            contenido = ft.Column(
                [
                    ft.Icon(name="movie" if tipo == "video" else "audiotrack", color=ACCENT, size=54),
                    ft.Text(
                        "Guarda el archivo para reproducirlo en este equipo."
                        if self.is_remote
                        else "La reproducción multimedia se abre en el navegador.",
                        size=12,
                        color=MUTED,
                    ),
                    ft.ElevatedButton(
                        "Guardar en Descargas" if self.is_remote else "Reproducir",
                        icon=ft.icons.DOWNLOAD if self.is_remote else ft.icons.PLAY_CIRCLE_OUTLINE,
                        bgcolor=ACCENT_STRONG,
                        color="#FFFFFF",
                        on_click=lambda e: self._open_url(self._file_url(item["path"], inline=not self.is_remote)),
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            )
        elif tipo == "pdf":
            contenido = ft.Column(
                [
                    ft.Icon(name=ft.icons.PICTURE_AS_PDF, color=DANGER, size=54),
                    ft.ElevatedButton(
                        "Guardar PDF" if self.is_remote else "Abrir PDF",
                        icon=ft.icons.DOWNLOAD if self.is_remote else ft.icons.OPEN_IN_NEW,
                        bgcolor=ACCENT_STRONG,
                        color="#FFFFFF",
                        on_click=lambda e: self._open_url(self._file_url(item["path"], inline=not self.is_remote)),
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            )
        elif tipo in {"text", "code"} or item.get("editable"):
            datos = self._request("GET", "/api/files/read", params={"path": item["path"]})
            texto = datos["content"] if datos else "(No se pudo cargar la vista previa)"
            contenido = ft.Container(
                height=460,
                border_radius=8,
                bgcolor=SURFACE_ALT,
                padding=14,
                content=ft.Column(
                    [ft.Text(texto[:200000], size=12, color=TEXT, selectable=True, font_family="monospace")],
                    scroll=ft.ScrollMode.AUTO,
                ),
            )
        else:
            contenido = ft.Column(
                [
                    ft.Icon(name=icon_for_kind(tipo), color=MUTED, size=54),
                    ft.Text("Vista previa no disponible para este formato.", size=12, color=MUTED),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            )
        acciones = [
            ft.TextButton("Cerrar", on_click=lambda e: self.page.close(dialogo)),
            ft.ElevatedButton("Descargar", icon=ft.icons.DOWNLOAD, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=lambda e: self._open_url(self._file_url(item["path"]))),
        ]
        if item.get("editable") and not item["is_dir"]:
            acciones.insert(0, ft.OutlinedButton("Editar", icon=ft.icons.EDIT, on_click=lambda e: (self.page.close(dialogo), self.show_editor(item))))
        dialogo = ft.AlertDialog(
            modal=False,
            title=ft.Text(item["name"], color=TEXT, size=16),
            content=ft.Container(width=760, content=contenido),
            actions=acciones,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    # Editor de texto con guardado y versiones anteriores.
    def show_editor(self, item: dict) -> None:
        datos = self._request("GET", "/api/files/read", params={"path": item["path"]})
        if datos is None:
            return
        editor = ft.TextField(
            value=datos["content"],
            multiline=True,
            min_lines=18,
            max_lines=26,
            text_size=13,
            color=TEXT,
            bgcolor=SURFACE_ALT,
            border_color=BORDER,
            focused_border_color=ACCENT,
            border_radius=10,
            content_padding=14,
            text_style=ft.TextStyle(font_family="monospace", size=13),
        )

        def save(e: ft.ControlEvent) -> None:
            resultado = self._request("PUT", "/api/files/write", json={"path": item["path"], "content": editor.value or ""})
            if resultado is not None:
                self.page.close(dialogo)
                self._notify("Archivo guardado")
                self.refresh_listing(self.current_path)

        dialogo = ft.AlertDialog(
            modal=False,
            title=ft.Text(f"Editar · {item['name']}", color=TEXT, size=16),
            content=ft.Container(width=780, content=editor),
            actions=[
                ft.TextButton(
                    "Historial",
                    icon=ft.icons.HISTORY,
                    on_click=lambda e: self._show_file_versions(item, dialogo),
                ),
                ft.TextButton("Cancelar", on_click=lambda e: self.page.close(dialogo)),
                ft.ElevatedButton("Guardar", icon=ft.icons.SAVE, bgcolor=SUCCESS, color="#FFFFFF", on_click=save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    def _show_file_versions(self, item: dict, editor_dialog: ft.AlertDialog) -> None:
        datos = self._request("GET", "/api/files/versions", params={"path": item["path"]})
        if datos is None:
            return
        versiones = datos.get("versions", [])
        filas: list[ft.Control] = []

        def delete_one(version_id: str) -> None:
            resultado = self._request(
                "DELETE",
                f"/api/files/versions/{version_id}",
                params={"path": item["path"]},
            )
            if resultado is not None:
                self.page.close(dialogo_historial)
                self._notify("Versión eliminada")
                self._show_file_versions(item, editor_dialog)

        def clear_all(e: ft.ControlEvent) -> None:
            def confirm(ev: ft.ControlEvent) -> None:
                resultado = self._request(
                    "DELETE", "/api/files/versions", params={"path": item["path"]}
                )
                self.page.close(dialogo_confirmacion)
                if resultado is not None:
                    self.page.close(dialogo_historial)
                    self._notify("Historial eliminado")
                    self._show_file_versions(item, editor_dialog)

            dialogo_confirmacion = ft.AlertDialog(
                modal=True,
                title=ft.Text("Borrar todo el historial", color=TEXT),
                content=ft.Text(
                    f"Se eliminarán todas las versiones guardadas de {item['name']}. Esta acción no se puede deshacer.",
                    color=MUTED,
                    size=13,
                ),
                actions=[
                    ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialogo_confirmacion)),
                    ft.ElevatedButton("Borrar todo", icon=ft.icons.DELETE_OUTLINE, bgcolor=DANGER, color="#FFFFFF", on_click=confirm),
                ],
            )
            self.page.open(dialogo_confirmacion)

        if not versiones:
            filas.append(ft.Text("Todavía no hay versiones guardadas.", color=MUTED, size=12))
        for version in versiones:
            marca = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(version["created"]))
            filas.append(
                ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(marca, color=TEXT, size=12),
                                ft.Text(human_size(version["size"]), color=MUTED, size=11),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.OutlinedButton(
                            "Restaurar",
                            icon=ft.icons.RESTORE,
                            on_click=lambda e, version_id=version["id"]: restore(version_id),
                        ),
                        ft.IconButton(
                            icon=ft.icons.DELETE_OUTLINE,
                            tooltip="Borrar esta versión",
                            icon_color=DANGER,
                            on_click=lambda e, version_id=version["id"]: delete_one(version_id),
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )

        def restore(version_id: str) -> None:
            resultado = self._request(
                "POST",
                "/api/files/versions/restore",
                params={"path": item["path"], "version_id": version_id},
            )
            if resultado is not None:
                self.page.close(dialogo_historial)
                self.page.close(editor_dialog)
                self._notify("Versión restaurada")
                self.refresh_listing(self.current_path)

        dialogo_historial = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Versiones · {item['name']}", color=TEXT),
            content=ft.Container(width=440, content=ft.Column(filas, tight=True, spacing=10)),
            actions=[
                ft.TextButton("Borrar todo", icon=ft.icons.DELETE_SWEEP, on_click=clear_all)
                if versiones
                else ft.Container(),
                ft.TextButton("Cerrar", on_click=lambda e: self.page.close(dialogo_historial)),
            ],
        )
        self.page.open(dialogo_historial)

    def show_rename(self, item: dict) -> None:
        campo = self._field("Nuevo nombre", item["name"], autofocus=True)

        def do_rename(e: ft.ControlEvent) -> None:
            nombre = (campo.value or "").strip()
            if not nombre:
                return
            resultado = self._request("PUT", "/api/files/rename", json={"path": item["path"], "new_name": nombre})
            if resultado is not None:
                self.page.close(dialogo)
                self._notify("Elemento renombrado")
                self.refresh_listing(self.current_path)

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Renombrar", color=TEXT),
            content=campo,
            actions=[
                ft.TextButton("Cancelar", on_click=lambda e: self.page.close(dialogo)),
                ft.ElevatedButton("Renombrar", bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=do_rename),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        campo.on_submit = do_rename
        self.page.open(dialogo)

    def confirm_delete(self, item: dict) -> None:
        def do_delete(e: ft.ControlEvent) -> None:
            resultado = self._request("DELETE", "/api/files", params={"path": item["path"]})
            self.page.close(dialogo)
            if resultado is not None:
                self._notify("Elemento eliminado")
                self.refresh_listing(self.current_path)

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Eliminar", color=TEXT),
            content=ft.Text(f"¿Eliminar «{item['name']}» de forma permanente?", color=MUTED, size=13),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda e: self.page.close(dialogo)),
                ft.ElevatedButton("Eliminar", bgcolor=DANGER, color="#FFFFFF", on_click=do_delete),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    def _new_folder(self, e: ft.ControlEvent) -> None:
        campo = self._field("Nombre de la carpeta", autofocus=True)

        def create(ev: ft.ControlEvent) -> None:
            nombre = (campo.value or "").strip()
            if not nombre:
                return
            resultado = self._request("POST", "/api/files/mkdir", json={"path": self.current_path, "name": nombre})
            if resultado is not None:
                self.page.close(dialogo)
                self._notify("Carpeta creada")
                self.refresh_listing(self.current_path)

        campo.on_submit = create
        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Nueva carpeta", color=TEXT),
            content=campo,
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialogo)),
                ft.ElevatedButton("Crear", icon=ft.icons.CREATE_NEW_FOLDER, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=create),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    def _new_text_file(self, e: ft.ControlEvent) -> None:
        campo_nombre = self._field("Nombre del archivo", "texto", autofocus=True)
        campo_contenido = self._field(
            "Escribe tu texto",
            multiline=True,
            min_lines=10,
            max_lines=18,
            text_style=ft.TextStyle(font_family="monospace", size=13),
            expand=True,
        )

        def save(ev: ft.ControlEvent) -> None:
            nombre = (campo_nombre.value or "").strip()
            if nombre.lower().endswith(".txt"):
                nombre = nombre[:-4]
            if not nombre or nombre in {".", ".."} or any(caracter in nombre for caracter in ("/", "\\", "\x00")):
                self._notify("Escribe un nombre válido, sin rutas ni barras.", True)
                return
            nombre_archivo = f"{nombre}.txt"
            listado = self._request("GET", "/api/files", params={"path": self.current_path})
            if listado is None:
                return
            if any(elemento.get("name", "").casefold() == nombre_archivo.casefold() for elemento in listado.get("items", [])):
                self._notify(f"Ya existe «{nombre_archivo}» en esta carpeta.", True)
                return
            ruta = f"{self.current_path}/{nombre_archivo}".lstrip("/")
            resultado = self._request(
                "PUT",
                "/api/files/write",
                json={"path": ruta, "content": campo_contenido.value or ""},
            )
            if resultado is not None:
                self.page.close(dialogo)
                self._notify(f"{nombre_archivo} creado")
                self.refresh_listing(self.current_path)

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Escribir archivo de texto", color=TEXT, size=16),
            content=ft.Container(
                width=640,
                height=390,
                content=ft.Column([campo_nombre, campo_contenido], spacing=14, expand=True),
            ),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialogo)),
                ft.ElevatedButton("Crear .txt", icon=ft.icons.SAVE, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    def _download_zip(self, e: ft.ControlEvent) -> None:
        self._open_url(self._zip_url(self.current_path))

    def _pick_upload(self, e: ft.ControlEvent) -> None:
        if sys.platform.startswith("linux"):
            self._show_file_picker()
            return
        self.file_picker.pick_files(allow_multiple=True, dialog_title="Seleccionar archivos para subir")

    # Cola de subidas con progreso y reanudación.
    def _on_files_picked(self, e: ft.FilePickerResultEvent) -> None:
        if not e.files:
            return
        rutas = [campo.path for campo in e.files if campo.path]
        if rutas and not bool(getattr(self.page, "web", False)):
            threading.Thread(target=self._upload_worker, args=(rutas,), daemon=True).start()
            return
        if not bool(getattr(self.page, "web", False)):
            self._notify("El selector no devolvió rutas de archivo", True)
            return
        self.web_upload_queue.clear()
        self.web_upload_results.clear()
        self.web_upload_remaining = len(e.files)
        subidas = []
        try:
            for elegido in e.files:
                sufijo = Path(elegido.name).suffix[:20]
                nombre_guardado = f"{uuid.uuid4().hex}{sufijo}"
                self.web_upload_queue.append(self.web_upload_dir / nombre_guardado)
                subidas.append(
                    ft.FilePickerUploadFile(
                        name=elegido.name,
                        upload_url=self.page.get_upload_url(nombre_guardado, 3600),
                    )
                )
        except Exception as excepcion:
            self.web_upload_queue.clear()
            self.web_upload_remaining = 0
            self._notify(f"No se pudieron preparar los archivos: {excepcion}", True)
            return
        self.upload_active = True
        self.upload_done = False
        self.upload_error = None
        self.upload_label = f"Preparando {len(subidas)} archivo(s)…"
        self.upload_speed = "Esperando la transferencia"
        self.upload_progress = 0.0
        self.file_picker.upload(subidas)

    def _on_file_uploaded(self, e: ft.FilePickerUploadEvent) -> None:
        if e.error:
            self.upload_error = f"No se pudo recibir {e.file_name}: {e.error}"
            self.upload_active = False
            self.web_upload_queue.clear()
            self.web_upload_remaining = 0
            return
        progreso = e.progress or 0.0
        if progreso < 1:
            completado = len(self.web_upload_results)
            self.upload_progress = (completado + progreso) / max(completado + self.web_upload_remaining, 1)
            self.upload_label = e.file_name
            self.upload_speed = "Recibiendo desde el navegador…"
            return
        if not self.web_upload_queue:
            return
        self.web_upload_results.append(self.web_upload_queue.popleft())
        self.web_upload_remaining -= 1
        if self.web_upload_remaining <= 0:
            seleccionado = [str(ruta) for ruta in self.web_upload_results]
            self.upload_label = "Guardando en la carpeta compartida…"
            self.upload_speed = "Subiendo a Vortex"
            threading.Thread(target=self._upload_worker, args=(seleccionado,), daemon=True).start()

    def _upload_worker(self, paths: list[str]) -> None:
        ruta_destino = self.current_path
        self.upload_target_path = ruta_destino
        self.upload_cancel.clear()
        self.upload_cancellable = False
        self.upload_active = True
        self.upload_done = False
        self.upload_error = None
        for ruta in paths:
            if self.upload_cancel.is_set():
                break
            self.upload_progress = 0.0
            self.upload_label = Path(ruta).name
            self.upload_speed = ""
            try:
                self._upload_one(Path(ruta), ruta_destino)
            except Exception as excepcion:
                self.upload_error = str(excepcion)
                break
            finally:
                local = Path(ruta)
                if local.parent == self.web_upload_dir:
                    try:
                        local.unlink(missing_ok=True)
                    except OSError:
                        pass
        self.upload_active = False
        if self.upload_cancel.is_set():
            self.upload_label = "Subida cancelada"
            self.upload_speed = ""
            self.upload_done = True
        elif self.upload_error is None:
            self.upload_progress = 1.0
            self.upload_speed = "Completado"
            self.upload_done = True

    def _upload_one(self, local: Path, target_dir: str) -> None:
        if self.http is None:
            raise RuntimeError("Cliente HTTP no inicializado")
        tamano = local.stat().st_size
        if tamano <= self.config.direct_upload_limit:
            self.upload_cancellable = False
            self.upload_speed = "Subiendo…"
            with open(local, "rb") as manejador:
                archivos = {"file": (local.name, manejador, "application/octet-stream")}
                respuesta = self.http.post(
                    "/api/files/upload",
                    files=archivos,
                    data={"target_dir": target_dir},
                    headers=self._auth_headers(),
                    timeout=None,
                )
            if respuesta.status_code >= 400:
                raise RuntimeError(self._response_error(respuesta))
            self.upload_progress = 1.0
            return
        self.upload_cancellable = True
        huella = hashlib.sha256()
        with open(local, "rb") as origen:
            for bloque in iter(lambda: origen.read(1024 * 1024), b""):
                huella.update(bloque)
        clave_reanudacion = hashlib.sha256(
            f"{self.base_url}|{target_dir}|{local.name}|{tamano}|{huella.hexdigest()}".encode()
        ).hexdigest()
        archivo_estado = app_data_dir() / "resumable_uploads.json"
        try:
            subidas_guardadas = json.loads(archivo_estado.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            subidas_guardadas = {}
        if not isinstance(subidas_guardadas, dict):
            subidas_guardadas = {}
        guardado = subidas_guardadas.get(clave_reanudacion, {})
        id_subida = guardado.get("upload_id")
        estado = None
        if id_subida:
            try:
                respuesta = self.http.get(
                    f"/api/files/upload/{id_subida}", headers=self._auth_headers(), timeout=15
                )
                if respuesta.status_code == 200:
                    estado = respuesta.json()
                    if (
                        estado.get("size") != tamano
                        or estado.get("filename") != local.name
                        or estado.get("target_dir") != target_dir
                    ):
                        estado = None
            except httpx.HTTPError:
                estado = None
        if estado is None:
            if id_subida:
                subidas_guardadas.pop(clave_reanudacion, None)
            inicial = self.http.post(
                "/api/files/upload/init",
                json={"filename": local.name, "size": tamano, "target_dir": target_dir, "chunk_size": self.config.chunk_size},
                headers=self._auth_headers(),
                timeout=30,
            )
            if inicial.status_code >= 400:
                raise RuntimeError(self._response_error(inicial))
            informacion = inicial.json()
            id_subida = informacion["upload_id"]
            estado = informacion
        subidas_guardadas[clave_reanudacion] = {"upload_id": id_subida}
        archivo_estado.parent.mkdir(parents=True, exist_ok=True)
        estado_temporal = archivo_estado.with_suffix(".tmp")
        estado_temporal.write_text(json.dumps(subidas_guardadas), encoding="utf-8")
        estado_temporal.replace(archivo_estado)
        tamano_bloque = estado["chunk_size"]
        cantidad = estado["chunk_count"]
        recibidos = set(estado.get("received", []))
        enviado = sum(
            min(tamano_bloque, max(tamano - indice * tamano_bloque, 0)) for indice in recibidos
        )
        self.upload_progress = enviado / max(tamano, 1)
        enviados_sesion = 0
        iniciado = time.time()
        with open(local, "rb") as manejador:
            for indice in range(cantidad):
                if self.upload_cancel.is_set():
                    return
                if indice in recibidos:
                    continue
                bloque = manejador.read(tamano_bloque)
                ultimo_error = None
                for _ in range(3):
                    try:
                        respuesta = self.http.put(
                            f"/api/files/upload/{id_subida}/{indice}",
                            content=bloque,
                            headers=self._auth_headers(),
                            timeout=None,
                        )
                        if respuesta.status_code < 400:
                            ultimo_error = None
                            break
                        ultimo_error = self._response_error(respuesta)
                    except httpx.HTTPError as excepcion:
                        ultimo_error = str(excepcion)
                if ultimo_error:
                    raise RuntimeError(ultimo_error)
                enviado += len(bloque)
                enviados_sesion += len(bloque)
                self.upload_progress = enviado / max(tamano, 1)
                transcurrido = max(time.time() - iniciado, 0.001)
                self.upload_speed = human_speed(enviados_sesion / transcurrido)
        completar = self.http.post(f"/api/files/upload/{id_subida}/complete", headers=self._auth_headers(), timeout=300)
        if completar.status_code >= 400:
            raise RuntimeError(self._response_error(completar))
        subidas_guardadas.pop(clave_reanudacion, None)
        estado_temporal.write_text(json.dumps(subidas_guardadas), encoding="utf-8")
        estado_temporal.replace(archivo_estado)

    def _response_error(self, response: httpx.Response) -> str:
        try:
            detalle = response.json().get("detail", response.text)
            if isinstance(detalle, dict):
                return detalle.get("message", str(detalle))
            return str(detalle)
        except Exception:
            return response.text or f"HTTP {response.status_code}"

    def _cancel_upload(self, e: ft.ControlEvent | None = None) -> None:
        if self.upload_cancellable:
            self.upload_cancel.set()

    def _refresh_upload_widget(self) -> None:
        if self.upload_widget is None:
            return
        if self.upload_active:
            self.upload_widget.visible = True
            if self.upload_cancel_button is not None:
                self.upload_cancel_button.visible = self.upload_cancellable
            self.upload_name_text.value = self.upload_label
            self.upload_speed_text.value = self.upload_speed
            self.upload_bar.value = self.upload_progress
            self.page.update()
            return
        if self.upload_error:
            mensaje = self.upload_error
            self.upload_error = None
            self.upload_done = False
            self.upload_widget.visible = False
            self.page.update()
            self._notify(f"Error de subida: {mensaje}", True)
            if self.current_path == self.upload_target_path:
                self.refresh_listing(self.upload_target_path)
            return
        if self.upload_done:
            self.upload_done = False
            self.upload_widget.visible = False
            self.page.update()
            mensaje = (
                "Subida cancelada; puedes reanudarla al seleccionar el mismo archivo."
                if self.upload_cancel.is_set()
                else "Subida completada"
            )
            self.upload_cancel.clear()
            self._notify(mensaje)
            if self.current_path == self.upload_target_path:
                self.refresh_listing(self.upload_target_path)

    # Paleta rápida de búsqueda y comandos.
    def open_palette(self) -> None:
        if self.palette_dialog is not None:
            return
        self.palette_input = ft.TextField(
            hint_text="Escribe para buscar · type:video  size:>1gb  ext:pdf",
            prefix_icon=ft.icons.SEARCH,
            autofocus=True,
            text_size=14,
            color=TEXT,
            hint_style=ft.TextStyle(color=MUTED, size=12),
            bgcolor=SURFACE_ALT,
            border_color=BORDER,
            focused_border_color=ACCENT,
            border_radius=10,
            content_padding=ft.padding.symmetric(horizontal=12, vertical=14),
            on_change=self._palette_change,
            on_submit=self._palette_submit,
        )
        self.palette_results = ft.ListView(expand=True, spacing=2, height=360)
        self.palette_dialog = ft.AlertDialog(
            modal=True,
            content=ft.Container(
                width=680,
                content=ft.Column(
                    [
                        ft.Row([ft.Icon(name=ft.icons.BOLT, color=ACCENT, size=18), ft.Text("HizSearch", size=13, weight=ft.FontWeight.BOLD, color=ACCENT)]),
                        self.palette_input,
                        self.palette_results,
                    ],
                    spacing=10,
                    tight=True,
                ),
            ),
            actions=[
                ft.Text("[Ctrl+K] abrir · [Esc] cerrar · Enter busca en el servidor", size=10, color=MUTED),
            ],
            actions_alignment=ft.MainAxisAlignment.START,
        )
        self.page.open(self.palette_dialog)
        self._palette_load("")

    def _close_palette(self) -> None:
        if self.palette_dialog is not None:
            try:
                self.page.close(self.palette_dialog)
            except Exception:
                pass
        self.palette_dialog = None
        self.palette_input = None
        self.palette_results = None

    def _palette_change(self, e: ft.ControlEvent) -> None:
        self._palette_load((self.palette_input.value or "").strip())

    def _palette_submit(self, e: ft.ControlEvent) -> None:
        self._palette_load((self.palette_input.value or "").strip())

    def _palette_load(self, query: str) -> None:
        if self.palette_results is None:
            return
        if query:
            datos = self._request("GET", "/api/files/search", params={"path": self.current_path, "q": query, "limit": 60})
            elementos = datos["items"] if datos else []
        else:
            datos = self._request("GET", "/api/files", params={"path": self.current_path})
            elementos = datos["items"] if datos else []
        self.palette_results.controls.clear()
        if not elementos:
            self.palette_results.controls.append(ft.Container(padding=20, content=ft.Text("Sin coincidencias", size=12, color=MUTED)))
        for elemento in elementos[:60]:
            self.palette_results.controls.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    border_radius=8,
                    on_click=lambda e, it=elemento: self._palette_open(it),
                    on_hover=self._hover_row,
                    content=ft.Row(
                        [
                            ft.Icon(name=icon_for_kind(elemento["kind"]), color=color_for_kind(elemento["kind"]), size=18),
                            ft.Text(elemento["name"], size=13, color=TEXT, expand=True, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text("Carpeta" if elemento["is_dir"] else human_size(elemento["size"]), size=11, color=MUTED),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=10,
                    ),
                )
            )
        try:
            self.page.update()
        except Exception:
            pass

    def _palette_open(self, item: dict) -> None:
        self._close_palette()
        self.open_item(item)

    # Consola de auditoría en vivo para el host.
    def _open_logs(self, e: ft.ControlEvent) -> None:
        if self.role != "host":
            self._notify("La consola de logs solo está disponible para el host", True)
            return
        self.logs_list = ft.ListView(expand=True, spacing=1, height=440, auto_scroll=True)
        self.logs_count_seen = -1
        self.logs_dialog_open = True
        dialogo = ft.AlertDialog(
            modal=False,
            title=ft.Row([ft.Icon(name=ft.icons.TERMINAL, color=ACCENT, size=18), ft.Text("Consola de auditoría en tiempo real", color=TEXT, size=15)]),
            content=ft.Container(width=860, content=self.logs_list),
            actions=[ft.TextButton("Cerrar", on_click=lambda ev: self._close_logs(dialogo))],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)
        self._refresh_logs()

    def _close_logs(self, dialog: ft.AlertDialog) -> None:
        self.logs_dialog_open = False
        self.page.close(dialog)

    def _refresh_logs(self) -> None:
        if self.logs_list is None:
            return
        registros = list(self.log_buffer)[-200:]
        if len(registros) == self.logs_count_seen:
            return
        self.logs_count_seen = len(registros)
        self.logs_list.controls.clear()
        colores = {"INFO": MUTED, "WARNING": WARNING, "ERROR": DANGER, "SECURITY": "#F472B6"}
        for registro in registros:
            severidad = registro.get("severity", "INFO")
            linea = f"{registro.get('timestamp', '')}  {severidad:<8} {registro.get('action', '')}  {registro.get('client_ip', '')}  {registro.get('target_path', '')}  {registro.get('status_code', '')}"
            self.logs_list.controls.append(ft.Text(linea, size=10, color=colores.get(severidad, MUTED), font_family="monospace", selectable=True))
        try:
            self.page.update()
        except Exception:
            pass

    # Parada ordenada del servidor y limpieza de recursos.
    def _stop_server(self, e: ft.ControlEvent) -> None:
        def do_stop(ev: ft.ControlEvent) -> None:
            self.page.close(dialogo)
            if self.log_unsubscribe:
                try:
                    self.log_unsubscribe()
                except Exception:
                    pass
            self.log_unsubscribe = None
            self.server.stop()
            if self.http is not None:
                self.http.close()
            self.http = None
            self.token = None
            self.role = None
            self.base_url = ""
            self.current_path = ""
            self._notify("Servidor detenido")
            self.show_setup()

        dialogo = ft.AlertDialog(
            modal=True,
            title=ft.Text("Detener servidor", color=TEXT),
            content=ft.Text("Se cerrarán las conexiones y el acceso a los archivos quedará suspendido.", color=MUTED, size=13),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialogo)),
                ft.ElevatedButton("Detener", bgcolor=DANGER, color="#FFFFFF", on_click=do_stop),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialogo)

    def _on_keyboard(self, e: ft.KeyboardEvent) -> None:
        clave = (e.key or "").lower()
        if e.ctrl and clave == "k":
            self.open_palette()
        elif clave == "escape":
            if self.palette_dialog is not None:
                self._close_palette()
        elif e.ctrl and clave == "r" and self.http is not None:
            self.refresh_listing(self.current_path)


def _libmpv_available() -> bool:
    if not sys.platform.startswith("linux"):
        return True
    import ctypes

    try:
        ctypes.CDLL("libmpv.so.1")
        return True
    except OSError:
        return False


def _enable_flet_web_compat() -> None:
    import fastapi

    original = fastapi.FastAPI.__init__
    if getattr(original, "_vortex_compat", False):
        return

    def compatible(self, *args, **kwargs):
        kwargs.pop("openapi_prefix", None)
        original(self, *args, **kwargs)

    compatible._vortex_compat = True
    fastapi.FastAPI.__init__ = compatible


def run_app() -> None:
    objetivo = lambda page: VortexUI(page).start()
    if not _libmpv_available():
        print(
            "Vortex: falta libmpv.so.1, necesario para la ventana nativa de Flet en Linux.\n"
            "Instala la biblioteca que proporcione libmpv.so.1 y vuelve a ejecutar.\n"
            "Mientras tanto se abrirá la interfaz en el navegador.",
            file=sys.stderr,
            flush=True,
        )
        _enable_flet_web_compat()
        ft.app(
            target=objetivo,
            view=ft.AppView.WEB_BROWSER,
            upload_dir=str(app_data_dir() / "browser-picker"),
        )
        return
    ft.app(target=objetivo)
