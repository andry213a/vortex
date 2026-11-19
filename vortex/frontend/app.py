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


class _VortexServiceListener(ServiceListener):
    def __init__(self, app: VortexUI) -> None:
        self.app = app

    def add_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self.app._resolve_discovered_server(zeroconf, service_type, name)

    def update_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self.app._resolve_discovered_server(zeroconf, service_type, name)

    def remove_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
        self.app._remove_discovered_server(name)


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
            icon_name = "vortex.ico" if sys.platform.startswith("win") else "vortex.png"
            self.page.window.icon = str(self._brand_asset_path(icon_name))
            self.page.window.title_bar_hidden = True
            self.page.window.title_bar_buttons_hidden = True
            self.page.window.minimizable = True
            self.page.window.maximizable = True
            self.page.window.resizable = True
        self.page.on_keyboard_event = self._on_keyboard
        self.page.on_disconnect = self._on_disconnect

    def _window_button(self, icon: str, tooltip: str, action, close: bool = False) -> ft.Container:
        normal_color = DANGER if close else MUTED
        hover_bg = "#7F1D1D" if close else SURFACE_ALT
        button = ft.Container(
            width=44,
            height=46,
            alignment=ft.alignment.center,
            tooltip=tooltip,
            on_click=action,
            content=ft.Icon(name=icon, size=15, color=normal_color),
        )

        def on_hover(e: ft.ControlEvent) -> None:
            entering = str(getattr(e, "data", "")).lower() == "true"
            button.bgcolor = hover_bg if entering else None
            icon_color = "#FFFFFF" if (entering and close) else (TEXT if entering else normal_color)
            if isinstance(button.content, ft.Icon):
                button.content.color = icon_color
            try:
                button.update()
            except Exception:
                pass

        button.on_hover = on_hover
        return button

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
        channels = [int(ACCENT[index:index + 2], 16) for index in (1, 3, 5)]
        ACCENT_STRONG = "#" + "".join(f"{round(channel * 0.78):02X}" for channel in channels)

    def _show_appearance_settings(self, e: ft.ControlEvent | None = None) -> None:
        current = self.config.accent_color
        rgb = [int(current[index:index + 2], 16) for index in (1, 3, 5)]
        preview = ft.Container(width=52, height=36, border_radius=8, bgcolor=current)
        rgb_fields = [
            self._field(label, str(value), width=82, keyboard_type=ft.KeyboardType.NUMBER)
            for label, value in zip(("R", "G", "B"), rgb)
        ]
        sliders: list[ft.Slider] = []

        def update_color() -> None:
            values = []
            for field in rgb_fields:
                try:
                    value = int(field.value or "0")
                except ValueError:
                    value = 0
                values.append(max(0, min(255, value)))
            color = "#" + "".join(f"{value:02X}" for value in values)
            preview.bgcolor = color
            color_label.value = color
            for index, slider in enumerate(sliders):
                slider.value = values[index]
            try:
                self.page.update()
            except Exception:
                pass

        for index, field in enumerate(rgb_fields):
            field.on_change = lambda ev: update_color()

        for index, value in enumerate(rgb):
            sliders.append(
                ft.Slider(
                    min=0,
                    max=255,
                    divisions=255,
                    value=value,
                    active_color=ACCENT,
                    on_change=lambda ev, channel=index: set_channel(channel, ev.control.value),
                )
            )

        def set_channel(index: int, value: float) -> None:
            rgb_fields[index].value = str(round(value))
            update_color()

        def select_preset(color: str) -> None:
            for field, value in zip(rgb_fields, (int(color[i:i + 2], 16) for i in (1, 3, 5))):
                field.value = str(value)
            update_color()

        def apply(e: ft.ControlEvent) -> None:
            old_accent, old_strong = ACCENT, ACCENT_STRONG
            self.config.accent_color = preview.bgcolor
            self._set_accent_globals(self.config.accent_color)
            try:
                self.store.save(self.config)
            except OSError as exc:
                self._notify(f"No se pudo guardar el color: {exc}", True)
                return
            self._recolor_existing_controls(old_accent, old_strong)
            self.page.close(dialog)
            self._notify("Color de Vortex actualizado")

        presets = ft.Row(
            [
                ft.Container(
                    width=30,
                    height=30,
                    border_radius=15,
                    bgcolor=color,
                    tooltip=name,
                    on_click=lambda ev, selected=color: select_preset(selected),
                    border=ft.border.all(2, TEXT if color == current else BORDER),
                )
                for name, color in self.ACCENT_PRESETS.items()
            ],
            spacing=12,
        )
        color_label = ft.Text(current, color=TEXT, font_family="monospace")
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Color de la aplicación", color=TEXT),
            content=ft.Container(
                width=480,
                content=ft.Column(
                    [
                        ft.Text("Colores sugeridos", color=MUTED, size=12),
                        presets,
                        ft.Divider(color=BORDER),
                        ft.Text("Ajuste RGB", color=MUTED, size=12),
                        *[
                            ft.Row([ft.Text(label, width=20, color=TEXT), slider, field], spacing=8)
                            for label, slider, field in zip(("R", "G", "B"), sliders, rgb_fields)
                        ],
                        ft.Row([preview, color_label]),
                    ],
                    tight=True,
                    spacing=10,
                ),
            ),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialog)),
                ft.ElevatedButton("Aplicar", bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=apply),
            ],
        )
        self.page.open(dialog)

    def _recolor_existing_controls(self, old_accent: str, old_strong: str) -> None:
        seen: set[int] = set()

        def visit(control: ft.Control) -> None:
            if id(control) in seen:
                return
            seen.add(id(control))
            for attribute in ("color", "bgcolor", "active_color", "border_color", "focused_border_color"):
                try:
                    value = getattr(control, attribute)
                    if value == old_accent:
                        setattr(control, attribute, ACCENT)
                    elif value == old_strong:
                        setattr(control, attribute, ACCENT_STRONG)
                except Exception:
                    pass
            for attribute in ("content", "controls"):
                try:
                    children = getattr(control, attribute)
                    if isinstance(children, ft.Control):
                        visit(children)
                    elif isinstance(children, list):
                        for child in children:
                            if isinstance(child, ft.Control):
                                visit(child)
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

    def _title_bar(self) -> ft.Container:
        running = self.server.running or self.is_remote
        status_color = SUCCESS if running else MUTED
        status_text = "REMOTO" if self.is_remote else "EN LÍNEA" if running else "DETENIDO"
        brand = ft.Row(
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
                            ft.Container(width=6, height=6, border_radius=3, bgcolor=status_color),
                            ft.Text(status_text, size=9, color=status_color, weight=ft.FontWeight.W_600),
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
        drag_area = ft.WindowDragArea(
            content=ft.Container(content=brand, padding=ft.padding.only(left=14, right=10)),
            expand=True,
            maximizable=True,
        )
        return ft.Container(
            height=46,
            bgcolor=SURFACE,
            border=ft.border.only(bottom=ft.BorderSide(1, BORDER)),
            content=ft.Row(
                [drag_area, self._window_controls()],
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
            encoded_logo = base64.b64encode(cls._brand_asset_path().read_bytes()).decode("ascii")
        except OSError:
            return ft.Icon(name=ft.icons.BOLT, size=max(16, size - 6), color=ACCENT)
        return ft.Image(
            src_base64=encoded_logo,
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
        except Exception as exc:
            self.discovery_error = str(exc)

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
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
            if info is None or (info.properties or {}).get(b"auth") != b"vortex":
                return
            addresses = info.parsed_addresses()
            if not addresses:
                return
            address = next(
                (item for item in addresses if ipaddress.ip_address(item).version == 4),
                addresses[0],
            )
            formatted_address = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
            url = f"http://{formatted_address}:{info.port}"
            display_name = name.split(f".{service_type}", 1)[0]
            if display_name.startswith("Vortex on "):
                display_name = display_name.removeprefix("Vortex on ")
            with self.discovery_lock:
                previous_url = self.discovered_service_urls.get(name)
                if previous_url and previous_url != url:
                    self.discovered_servers.pop(previous_url, None)
                self.discovered_service_urls[name] = url
                self.discovered_servers[url] = {"name": display_name or "Servidor Vortex", "url": url}
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
            servers = tuple(sorted(
                ((item["name"], item["url"]) for item in self.discovered_servers.values()),
                key=lambda item: (item[0].casefold(), item[1]),
            ))
        if servers:
            status_message = f"{len(servers)} servidor(es) · actualizado {time.strftime('%H:%M:%S')}"
        elif self.discovery_error:
            status_message = "No se pudo buscar automáticamente. Puedes escribir la IP abajo."
        elif time.monotonic() - self.discovery_started_at < 8:
            status_message = "Buscando servidores Vortex en la red local…"
        else:
            status_message = "No se encontraron servidores. Puedes escribir la IP abajo."
        signature = (status_message, *(f"{name}|{url}" for name, url in servers))
        if signature == self.discovered_render_signature:
            return
        self.discovered_render_signature = signature
        control.controls.clear()
        status = self.discovered_status
        if status is not None:
            status.value = status_message
        if servers:
            for name, url in servers:
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
                                        ft.Text(name, color=TEXT, size=12, weight=ft.FontWeight.W_600),
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
        for candidate in (Path.home() / "documento", Path.home() / "Documents", Path.home()):
            if candidate.exists():
                return str(candidate)
        return str(Path.cwd())

    def _on_disconnect(self, e: ft.ControlEvent) -> None:
        self._stop_discovery()
        try:
            self.server.stop()
        except Exception:
            pass

    def _notify(self, message: str, error: bool = False) -> None:
        snack = ft.SnackBar(
            content=ft.Text(message, color=TEXT, size=13),
            bgcolor=DANGER if error else SURFACE,
            duration=4500,
        )
        try:
            self.page.open(snack)
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

        start_button = ft.ElevatedButton(
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

        form = ft.Column(
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
                        start_button,
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

        page_content = ft.Container(
            expand=True,
            bgcolor=BG,
            alignment=ft.alignment.center,
            content=ft.Column(
                [self._card(form)],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._set_root(page_content)

    def _show_connect_dialog(self, e: ft.ControlEvent | None = None) -> None:
        host_field = self._field("IP o dirección del servidor", value="", hint_text="192.168.1.25 o http://vortex.local")
        port_field = self._field("Puerto", value="8080", width=120, keyboard_type=ft.KeyboardType.NUMBER)
        error_text = ft.Text("", color=DANGER, size=12)
        self.discovered_status = ft.Text("Buscando servidores Vortex en la red local…", color=MUTED, size=11)
        self.discovered_servers_control = ft.Column(
            height=135,
            spacing=5,
            scroll=ft.ScrollMode.AUTO,
        )
        self.discovered_render_signature = None

        def connect(ev: ft.ControlEvent) -> None:
            raw_host = (host_field.value or "").strip()
            try:
                port = int((port_field.value or "8080").strip())
                if not 1 <= port <= 65535:
                    raise ValueError
            except ValueError:
                error_text.value = "El puerto debe ser un número entre 1 y 65535"
                self.page.update()
                return
            if not raw_host:
                error_text.value = "Escribe la IP o el nombre del servidor"
                self.page.update()
                return
            try:
                remote_url = self._normalize_remote_url(raw_host, port)
            except ValueError as exc:
                error_text.value = str(exc)
                self.page.update()
                return
            self.page.close(dialog)
            self._connect_remote(remote_url)

        host_field.on_submit = connect
        dialog = ft.AlertDialog(
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
                        host_field,
                        port_field,
                        error_text,
                    ],
                    tight=True,
                    spacing=12,
                ),
            ),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialog)),
                ft.ElevatedButton("Conectar", icon=ft.icons.LINK, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=connect),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.connect_dialog = dialog
        self.page.open(dialog)
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
            except Exception as exc:
                self.discovery_error = str(exc)
        self._refresh_discovered_servers()

    @staticmethod
    def _normalize_remote_url(value: str, default_port: int) -> str:
        raw = value if "://" in value else f"http://{value}"
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Usa una dirección http:// o https:// válida")
        if parsed.username or parsed.password:
            raise ValueError("No incluyas credenciales en la dirección del servidor")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Escribe solo la IP o el nombre del servidor, sin rutas adicionales")
        try:
            existing_port = parsed.port
        except ValueError as exc:
            raise ValueError("El puerto de la dirección no es válido") from exc
        netloc = parsed.netloc if existing_port is not None else f"{parsed.netloc}:{default_port}"
        return urlunsplit((parsed.scheme, netloc, "", "", ""))

    def _connect_remote(self, remote_url: str) -> None:
        client = httpx.Client(base_url=remote_url, timeout=httpx.Timeout(10.0, read=None))
        try:
            response = client.get("/api/auth/status")
            response.raise_for_status()
            auth_status = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            client.close()
            self._notify(f"No se pudo conectar con {remote_url}: {exc}", True)
            return
        self.http = client
        self.base_url = remote_url
        self.file_revision = 0
        self.file_sync_checked_at = 0.0
        self.is_remote = True
        self.token = None
        self.role = None
        self.remote_auth_mode = str(auth_status.get("auth_mode", "none"))
        self.remote_requires_login = bool(auth_status.get("requires_login"))
        self.remote_guest_write = bool(auth_status.get("guest_write", True))
        self._start_polling()
        if self.remote_requires_login:
            self.show_login()
            return
        try:
            response = client.get("/api/auth/me")
            response.raise_for_status()
            self.role = response.json().get("role", "guest_read")
        except (httpx.HTTPError, ValueError) as exc:
            self._disconnect_remote(None)
            self._notify(f"El servidor respondió, pero no permitió el acceso: {exc}", True)
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
        mode = self.auth_dropdown.value
        self.auth_extra.controls.clear()
        if mode == "auto":
            self.auth_extra.controls.append(
                ft.Row([self.token_field, self.regenerate_button], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)
            )
        elif mode == "manual":
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
        current = Path(self.dir_field.value or Path.home()).expanduser()
        if not current.is_dir():
            current = Path.home()
        self._show_local_picker(current, "folder")

    def _show_file_picker(self) -> None:
        current = Path(self.config.base_dir or Path.home()).expanduser()
        if self.current_path:
            current = current / self.current_path
        if not current.is_dir():
            current = Path.home()
        self._show_local_picker(current, "files")

    def _show_local_picker(self, current: Path, mode: str) -> None:
        self.folder_picker_path = current.resolve()
        self.folder_picker_mode = mode
        self.folder_picker_selected.clear()
        self.folder_picker_path_text = ft.Text("", size=12, color=TEXT, selectable=True, font_family="monospace")
        self.folder_picker_selection_text = ft.Text("", size=11, color=MUTED)
        self.folder_picker_list = ft.ListView(expand=True, spacing=2, controls=[])
        folder_mode = mode == "folder"
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Seleccionar carpeta raíz" if folder_mode else "Seleccionar archivos", color=TEXT),
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
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialog)),
                ft.ElevatedButton(
                    "Usar esta carpeta" if folder_mode else "Subir archivos",
                    icon=ft.icons.CHECK if folder_mode else ft.icons.UPLOAD_FILE,
                    bgcolor=ACCENT_STRONG,
                    color="#FFFFFF",
                    on_click=lambda ev: self._use_folder_picker_path(dialog) if folder_mode else self._use_file_picker_selection(dialog),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.folder_picker_dialog = dialog
        self._render_folder_picker()
        self.page.open(dialog)

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
            entries = list(self.folder_picker_path.iterdir())
            folders = sorted((entry for entry in entries if entry.is_dir()), key=lambda entry: entry.name.casefold())
            files = sorted((entry for entry in entries if entry.is_file()), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            self.folder_picker_list.controls.append(ft.Text(f"No se pudo leer esta carpeta: {exc}", color=DANGER, size=12))
            self.page.update()
            return
        if not folders and self.folder_picker_mode == "folder":
            self.folder_picker_list.controls.append(ft.Text("No hay subcarpetas aquí.", color=MUTED, size=12))
        for folder in folders:
            self.folder_picker_list.controls.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=8, vertical=6),
                    border_radius=8,
                    on_click=lambda ev, target=folder: self._open_folder_picker_path(target),
                    content=ft.Row(
                        [
                            ft.Icon(name=ft.icons.FOLDER, color=ACCENT, size=19),
                            ft.Text(folder.name, color=TEXT, size=13, expand=True, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Icon(name=ft.icons.CHEVRON_RIGHT, color=MUTED, size=18),
                        ],
                        spacing=8,
                    ),
                )
            )
        if self.folder_picker_mode == "files":
            for path in files:
                kind = file_kind(path.name, False)
                self.folder_picker_list.controls.append(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        border_radius=8,
                        content=ft.Row(
                            [
                                ft.Icon(name=icon_for_kind(kind), color=color_for_kind(kind), size=18),
                                ft.Checkbox(
                                    label=path.name,
                                    value=path in self.folder_picker_selected,
                                    expand=True,
                                    on_change=lambda ev, target=path: self._toggle_file_picker_selection(target, bool(ev.control.value)),
                                ),
                            ],
                            spacing=8,
                        ),
                    )
                )
            if not folders and not files:
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
        parent = self.folder_picker_path.parent
        if parent != self.folder_picker_path:
            self.folder_picker_path = parent
            self._render_folder_picker()

    def _use_folder_picker_path(self, dialog: ft.AlertDialog) -> None:
        self.dir_field.value = str(self.folder_picker_path)
        self.page.close(dialog)
        self.page.update()

    def _use_file_picker_selection(self, dialog: ft.AlertDialog) -> None:
        if not self.folder_picker_selected:
            self._notify("Selecciona uno o más archivos para subir", True)
            return
        paths = [str(path) for path in sorted(self.folder_picker_selected, key=lambda item: item.name.casefold())]
        self.page.close(dialog)
        threading.Thread(target=self._upload_worker, args=(paths,), daemon=True).start()

    def _on_dir_picked(self, e: ft.FilePickerResultEvent) -> None:
        if e.path:
            self.dir_field.value = e.path
            self.page.update()

    def _start_server(self, e: ft.ControlEvent) -> None:
        base = (self.dir_field.value or "").strip()
        if not base:
            self._notify("Selecciona una carpeta raíz", True)
            return
        try:
            port = int((self.port_field.value or "").strip())
        except ValueError:
            self._notify("El puerto debe ser un número entero entre 1 y 65535", True)
            return
        cfg = ServerConfig(
            base_dir=base,
            port=port,
            auth_mode=self.auth_dropdown.value,
            auto_token=(self.token_field.value or "").strip() or generate_auto_token(),
            guest_write=(self.guest_dropdown.value or "write") == "write",
            password_hash=self.config.password_hash,
            hostname=self.config.hostname,
            chunk_size=self.config.chunk_size,
            direct_upload_limit=self.config.direct_upload_limit,
        )
        if cfg.auth_mode == "manual":
            password = self.password_field.value or ""
            if password:
                cfg.manual_password = password
            elif self.config.password_hash:
                cfg.password_hash = self.config.password_hash
            else:
                self.setup_error.value = "El modo manual requiere una contraseña"
                self.page.update()
                return
        try:
            cfg.validate()
            validate_port(cfg.port)
        except (TypeError, ValueError) as exc:
            suggestion = suggest_free_port(cfg.port + 1) if "uso" in str(exc) else None
            text = str(exc)
            if suggestion:
                text = f"{text}. Sugerencia: {suggestion}"
            self.setup_error.value = text
            self._notify(text, True)
            return
        base_path = Path(cfg.base_dir).expanduser()
        if needs_elevation(base_path):
            self._confirm_elevation(cfg, base_path)
            return
        self._launch(cfg)

    def _confirm_elevation(self, cfg: ServerConfig, base_path: Path) -> None:
        def do_elevate(e: ft.ControlEvent) -> None:
            self.page.close(dialog)
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

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Se requieren permisos de administrador", color=TEXT),
            content=ft.Text(
                f"La carpeta seleccionada requiere privilegios elevados:\n{base_path}\n\n"
                "Si deniegas la elevación, Vortex no se cerrará: podrás elegir otra carpeta.",
                color=MUTED,
                size=13,
            ),
            actions=[
                ft.TextButton("Elegir otra carpeta", on_click=lambda e: self.page.close(dialog)),
                ft.ElevatedButton("Solicitar elevación", bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=do_elevate),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def _launch(self, cfg: ServerConfig) -> None:
        try:
            runtime = self.server.start(cfg)
        except (ServerStartError, ValueError) as exc:
            self._notify(str(exc), True)
            return
        except Exception as exc:
            self._notify(f"No se pudo iniciar el servidor ({type(exc).__name__}): {exc}", True)
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
        self.log_unsubscribe = runtime.audit.add_listener(self._audit_listener)
        self._start_polling()
        self._enter_app()

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
                    message, is_error = self.remote_download_notice
                    self.remote_download_notice = None
                    self._notify(message, is_error)
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
            response = await asyncio.to_thread(
                self.http.get,
                "/api/system/changes",
                params={"since": self.file_revision},
                headers=self._auth_headers(),
                timeout=2.5,
            )
        except httpx.HTTPError:
            return
        if response.status_code != 200:
            return
        try:
            data = response.json()
        except ValueError:
            return
        changes = data.get("changes") or []
        self.file_revision = int(data.get("revision", self.file_revision))
        if not changes:
            return
        path = self.current_path
        refresh = False
        for change in changes:
            removed = change.get("removed")
            if removed and (path == removed or path.startswith(removed + "/")):
                path = removed.rpartition("/")[0]
                refresh = True
            renamed = change.get("renamed")
            if renamed:
                old = renamed.get("from", "")
                if old and (path == old or path.startswith(old + "/")):
                    path = renamed.get("to", "") + path[len(old):]
                    refresh = True
            if path in (change.get("paths") or []):
                refresh = True
        if refresh:
            self.refresh_listing(path)

    def _enter_app(self) -> None:
        if self._auto_login():
            self.show_browser()
            return
        self.show_login()

    def _auto_login(self) -> bool:
        if self.config.auth_mode == "none":
            try:
                response = httpx.get(f"{self.base_url}/api/auth/me", timeout=10)
            except httpx.HTTPError:
                return False
            if response.status_code == 200:
                self.role = response.json().get("role")
                self.token = None
                return True
            return False
        secret = None
        if self.config.auth_mode == "auto":
            secret = self.config.auto_token
        elif self.config.auth_mode == "manual" and self.config.manual_password:
            secret = self.config.manual_password
        if not secret:
            return False
        return self._post_login(secret)

    def _post_login(self, secret: str) -> bool:
        if self.http is None:
            return False
        try:
            response = self.http.post("/api/auth/login", json={"secret": secret}, timeout=20)
        except httpx.HTTPError:
            return False
        if response.status_code != 200:
            return False
        data = response.json()
        self.token = data.get("access_token")
        self.role = data.get("role")
        if self.http is not None:
            self.http.headers["Authorization"] = f"Bearer {self.token}"
        return True

    def show_login(self, error: str = "") -> None:
        credential_label = "Token o contraseña del servidor" if self.is_remote else "Contraseña de administrador"
        password = self._field(credential_label, password=True, can_reveal_password=True, autofocus=True)
        error_text = ft.Text(error, color=DANGER, size=12)
        busy = ft.ProgressRing(width=18, height=18, visible=False, color=ACCENT)

        def attempt(e: ft.ControlEvent) -> None:
            if not password.value:
                error_text.value = "Introduce la contraseña"
                self.page.update()
                return
            busy.visible = True
            error_text.value = ""
            self.page.update()
            ok = self._post_login(password.value)
            busy.visible = False
            if ok:
                self.show_browser()
            else:
                error_text.value = "Credenciales inválidas o bloqueo temporal por intentos fallidos"
                self.page.update()

        def continue_as_guest(e: ft.ControlEvent | None = None) -> None:
            self.token = None
            guest_write = self.remote_guest_write if self.is_remote else self.config.guest_write
            self.role = "guest_write" if guest_write else "guest_read"
            if self.http is not None:
                self.http.headers.pop("Authorization", None)
            self.show_browser()

        password.on_submit = attempt
        secondary_action = (
            ft.TextButton("Cancelar", on_click=self._cancel_remote_login)
            if self.is_remote and self.remote_requires_login
            else ft.OutlinedButton("Continuar como invitado", on_click=continue_as_guest)
        )
        card = self._card(
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
                    password,
                    error_text,
                    ft.Row(
                        [
                            secondary_action,
                            busy,
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
                content=ft.Column([card], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
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

    def _request(self, method: str, url: str, **kwargs):
        if self.http is None:
            return None
        headers = dict(self._auth_headers())
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            response = self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            self._notify(f"Error de conexión: {exc}", True)
            return None
        if response.status_code == 401:
            self.token = None
            self.show_login("La sesión expiró. Vuelve a iniciar sesión.")
            return None
        if response.status_code >= 400:
            detail = response.text
            try:
                payload = response.json()
                detail = payload.get("detail", detail)
                if isinstance(detail, dict):
                    detail = detail.get("message", str(detail))
            except Exception:
                pass
            self._notify(str(detail), True)
            return None
        try:
            return response.json()
        except ValueError:
            return None

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

        badge_color = SUCCESS if self.role == "host" else ACCENT
        role_label = {"host": "HOST", "guest_write": "INVITADO · TOTAL", "guest_read": "INVITADO · LECTURA"}.get(self.role or "", "INVITADO")
        if self.is_remote and self.role == "host":
            role_label = "HOST REMOTO"
        header_actions: list[ft.Control] = []
        if not self.is_desktop:
            header_actions.append(
                ft.IconButton(
                    icon=ft.icons.PALETTE,
                    icon_color=ACCENT,
                    tooltip="Personalizar colores",
                    on_click=self._show_appearance_settings,
                )
            )
        if self.is_remote:
            if self.remote_auth_mode != "none" and self.role != "host":
                header_actions.append(
                    ft.IconButton(
                        icon=ft.icons.LOGIN,
                        icon_color=ACCENT,
                        tooltip="Iniciar sesión como anfitrión",
                        on_click=lambda ev: self.show_login(),
                    )
                )
            header_actions.append(
                ft.IconButton(
                    icon=ft.icons.LINK_OFF,
                    icon_color=DANGER,
                    tooltip="Desconectar del servidor",
                    on_click=self._disconnect_remote,
                )
            )
        else:
            if self.config.auth_mode != "none" and self.role != "host":
                header_actions.append(
                    ft.IconButton(
                        icon=ft.icons.LOGIN,
                        icon_color=ACCENT,
                        tooltip="Iniciar sesión como anfitrión",
                        on_click=lambda e: self.show_login(),
                    )
                )
            header_actions.extend(
                [
                    ft.TextButton(text="Acceso LAN", icon=ft.icons.WIFI_TETHERING, tooltip="Cómo entrar desde la red local", on_click=self._show_lan_help),
                    ft.IconButton(icon=ft.icons.TERMINAL, icon_color=MUTED, tooltip="Consola de logs (host)", on_click=self._open_logs),
                    ft.IconButton(icon=ft.icons.POWER_SETTINGS_NEW, icon_color=DANGER, tooltip="Detener servidor", on_click=self._stop_server),
                ]
            )
        header = ft.Container(
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
                                ft.Container(width=7, height=7, border_radius=4, bgcolor=badge_color),
                                ft.Text(role_label, size=10, color=badge_color, weight=ft.FontWeight.W_600),
                            ],
                            spacing=5,
                        ),
                    ),
                    ft.Container(expand=True),
                    self.search_field,
                    *header_actions,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        toolbar = ft.Container(
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

        body = ft.Container(expand=True, padding=ft.padding.symmetric(horizontal=18), content=self.listing)
        column = ft.Column([header, toolbar, body], expand=True, spacing=0)
        stack = ft.Stack([ft.Container(expand=True, content=column), self.upload_widget], expand=True)
        self._set_root(stack)
        self.refresh_listing(self.current_path)

    def _show_lan_help(self, e: ft.ControlEvent | None = None) -> None:
        port = self.config.port
        hostname = self.config.hostname.rstrip(".")
        address = local_ip()
        ip_url = f"http://{address}:{port}"
        named_url = f"http://{hostname}:{port}"
        qr_image = qrcode.make(ip_url)
        qr_buffer = io.BytesIO()
        qr_image.save(qr_buffer)
        qr_base64 = base64.b64encode(qr_buffer.getvalue()).decode("ascii")
        dialog = ft.AlertDialog(
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
                                            ft.Text(ip_url, color=TEXT, size=14, selectable=True, font_family="monospace"),
                                            ft.Divider(color=BORDER, height=8),
                                            ft.Text("Nombre mDNS (puede no funcionar en ChromeOS)", color=MUTED, size=11),
                                            ft.Text(named_url, color=MUTED, size=12, selectable=True, font_family="monospace"),
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
                            f"TCP del puerto {port} en Configuración > Desarrolladores > Linux > Reenvío "
                            "de puertos y, desde el otro dispositivo, usa la IP Wi-Fi del Chromebook.",
                            color=MUTED,
                            size=12,
                        ),
                    ],
                    tight=True,
                    spacing=14,
                ),
            ),
            actions=[
                ft.TextButton("Cerrar", on_click=lambda ev: self.page.close(dialog)),
                ft.ElevatedButton(
                    "Abrir IP aquí",
                    icon=ft.icons.OPEN_IN_BROWSER,
                    bgcolor=ACCENT_STRONG,
                    color="#FFFFFF",
                    on_click=lambda ev: (self.page.close(dialog), self._open_url(ip_url)),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def _on_search_submit(self, e: ft.ControlEvent) -> None:
        query = (self.search_field.value or "").strip()
        if not query:
            self.refresh_listing(self.current_path)
            return
        data = self._request("GET", "/api/files/search", params={"path": self.current_path, "q": query, "limit": 300})
        if data is None:
            return
        self._render_items(data["items"])
        suffix = " · resultados truncados" if data.get("truncated") else ""
        self.count_text.value = f"{data['total']} coincidencias{suffix}"
        self.page.update()

    def refresh_listing(self, path: str) -> None:
        data = self._request("GET", "/api/files", params={"path": path})
        if data is None:
            return
        self.current_path = data["path"]
        self.parent_path = data["parent"]
        self._render_items(data["items"])
        self.count_text.value = f"{data['total']} elementos"
        self._build_breadcrumb(self.current_path)
        self.page.update()

    def _build_breadcrumb(self, path: str) -> None:
        self.breadcrumb.controls.clear()
        root_button = ft.TextButton(
            text="Raíz",
            icon=ft.icons.HOME_ROUNDED,
            on_click=lambda e: self.refresh_listing(""),
        )
        self.breadcrumb.controls.append(root_button)
        if not path:
            return
        accumulated = []
        for part in path.split("/"):
            accumulated.append(part)
            target = "/".join(accumulated)
            self.breadcrumb.controls.append(ft.Text("/", size=12, color=MUTED))
            self.breadcrumb.controls.append(
                ft.TextButton(text=part, on_click=lambda e, t=target: self.refresh_listing(t))
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
        for item in items:
            self.listing.controls.append(self._build_row(item))

    def _build_row(self, item: dict) -> ft.Container:
        kind = item["kind"]
        subtitle = "Carpeta" if item["is_dir"] else human_size(item["size"])
        subtitle += f"  ·  {format_time(item['modified'])}"
        actions = []
        if item["is_dir"]:
            actions.append(ft.IconButton(icon=ft.icons.ARCHIVE, icon_size=18, icon_color=MUTED, tooltip="Descargar .zip", on_click=lambda e, it=item: self._open_url(self._zip_url(it["path"]))))
        else:
            actions.append(ft.IconButton(icon=ft.icons.DOWNLOAD, icon_size=18, icon_color=MUTED, tooltip="Descargar", on_click=lambda e, it=item: self._open_url(self._file_url(it["path"]))))
        if item.get("editable") and not item["is_dir"]:
            actions.append(ft.IconButton(icon=ft.icons.EDIT_OUTLINED, icon_size=18, icon_color=ACCENT, tooltip="Editar", on_click=lambda e, it=item: self.show_editor(it)))
        actions.append(ft.IconButton(icon=ft.icons.EDIT_NOTE, icon_size=18, icon_color=MUTED, tooltip="Renombrar", on_click=lambda e, it=item: self.show_rename(it)))
        actions.append(ft.IconButton(icon=ft.icons.DELETE_OUTLINE, icon_size=18, icon_color=DANGER, tooltip="Eliminar", on_click=lambda e, it=item: self.confirm_delete(it)))
        icon = ft.Container(
            width=38,
            height=38,
            border_radius=10,
            bgcolor=SURFACE,
            alignment=ft.alignment.center,
            content=ft.Icon(name=icon_for_kind(kind), color=color_for_kind(kind), size=20),
        )
        details = ft.Column(
            [
                ft.Text(item["name"], size=14, color=TEXT, weight=ft.FontWeight.W_500, overflow=ft.TextOverflow.ELLIPSIS, max_lines=1),
                ft.Text(subtitle, size=11, color=MUTED),
            ],
            spacing=1,
            expand=True,
        )
        open_target = lambda e, it=item: self.open_item(it)
        main = ft.Container(
            expand=True,
            on_click=open_target,
            content=ft.Row([icon, details], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )
        row = ft.Row(
            [
                main,
                ft.Row(actions, spacing=0),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=0,
        )
        return ft.Container(
            content=row,
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
        params = {"path": path}
        if inline:
            params["inline"] = "1"
        if self.token:
            params["token"] = self.token
        return f"{self.base_url}/api/files/download?{urlencode(params)}"

    def _zip_url(self, path: str) -> str:
        params = {"path": path}
        if self.token:
            params["token"] = self.token
        return f"{self.base_url}/api/files/zip?{urlencode(params)}"

    def _open_url(self, url: str) -> None:
        if self.is_remote:
            threading.Thread(target=self._download_remote_url, args=(url,), daemon=True).start()
            return
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._notify(f"No se pudo abrir el navegador: {exc}", True)

    def _download_remote_url(self, url: str) -> None:
        if self.http is None:
            self.remote_download_notice = ("La conexión con el servidor se cerró", True)
            return
        try:
            with self.http.stream("GET", url, headers=self._auth_headers(), timeout=None) as response:
                response.raise_for_status()
                disposition = Message()
                if response.headers.get("content-disposition"):
                    disposition["content-disposition"] = response.headers["content-disposition"]
                filename = disposition.get_filename()
                if not filename:
                    query = parse_qs(urlsplit(url).query)
                    file_path = query.get("path", [""])[0]
                    filename = Path(unquote(file_path)).name or "descarga"
                    if "/zip" in url and not filename.endswith(".zip"):
                        filename += ".zip"
                filename = Path(filename).name.replace("\x00", "") or "descarga"
                download_dir = Path.home() / "Downloads"
                download_dir.mkdir(parents=True, exist_ok=True)
                destination = download_dir / filename
                index = 1
                while destination.exists():
                    destination = download_dir / f"{Path(filename).stem}_{index}{Path(filename).suffix}"
                    index += 1
                temporary = destination.with_name(destination.name + ".part")
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
                temporary.replace(destination)
            self.remote_download_notice = (f"Guardado en {destination}", False)
        except Exception as exc:
            self.remote_download_notice = (f"No se pudo descargar el archivo: {exc}", True)

    def show_preview(self, item: dict) -> None:
        kind = item["kind"]
        content: ft.Control
        if kind == "image":
            content = ft.Container(
                height=460,
                alignment=ft.alignment.center,
                content=ft.Image(src=self._file_url(item["path"], inline=True), fit=ft.ImageFit.CONTAIN, border_radius=8),
            )
        elif kind in {"video", "audio"}:
            content = ft.Column(
                [
                    ft.Icon(name="movie" if kind == "video" else "audiotrack", color=ACCENT, size=54),
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
        elif kind == "pdf":
            content = ft.Column(
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
        elif kind in {"text", "code"} or item.get("editable"):
            data = self._request("GET", "/api/files/read", params={"path": item["path"]})
            text = data["content"] if data else "(No se pudo cargar la vista previa)"
            content = ft.Container(
                height=460,
                border_radius=8,
                bgcolor=SURFACE_ALT,
                padding=14,
                content=ft.Column(
                    [ft.Text(text[:200000], size=12, color=TEXT, selectable=True, font_family="monospace")],
                    scroll=ft.ScrollMode.AUTO,
                ),
            )
        else:
            content = ft.Column(
                [
                    ft.Icon(name=icon_for_kind(kind), color=MUTED, size=54),
                    ft.Text("Vista previa no disponible para este formato.", size=12, color=MUTED),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
            )
        actions = [
            ft.TextButton("Cerrar", on_click=lambda e: self.page.close(dialog)),
            ft.ElevatedButton("Descargar", icon=ft.icons.DOWNLOAD, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=lambda e: self._open_url(self._file_url(item["path"]))),
        ]
        if item.get("editable") and not item["is_dir"]:
            actions.insert(0, ft.OutlinedButton("Editar", icon=ft.icons.EDIT, on_click=lambda e: (self.page.close(dialog), self.show_editor(item))))
        dialog = ft.AlertDialog(
            modal=False,
            title=ft.Text(item["name"], color=TEXT, size=16),
            content=ft.Container(width=760, content=content),
            actions=actions,
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def show_editor(self, item: dict) -> None:
        data = self._request("GET", "/api/files/read", params={"path": item["path"]})
        if data is None:
            return
        editor = ft.TextField(
            value=data["content"],
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
            result = self._request("PUT", "/api/files/write", json={"path": item["path"], "content": editor.value or ""})
            if result is not None:
                self.page.close(dialog)
                self._notify("Archivo guardado")
                self.refresh_listing(self.current_path)

        dialog = ft.AlertDialog(
            modal=False,
            title=ft.Text(f"Editar · {item['name']}", color=TEXT, size=16),
            content=ft.Container(width=780, content=editor),
            actions=[
                ft.TextButton(
                    "Historial",
                    icon=ft.icons.HISTORY,
                    on_click=lambda e: self._show_file_versions(item, dialog),
                ),
                ft.TextButton("Cancelar", on_click=lambda e: self.page.close(dialog)),
                ft.ElevatedButton("Guardar", icon=ft.icons.SAVE, bgcolor=SUCCESS, color="#FFFFFF", on_click=save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def _show_file_versions(self, item: dict, editor_dialog: ft.AlertDialog) -> None:
        data = self._request("GET", "/api/files/versions", params={"path": item["path"]})
        if data is None:
            return
        versions = data.get("versions", [])
        rows: list[ft.Control] = []

        def delete_one(version_id: str) -> None:
            result = self._request(
                "DELETE",
                f"/api/files/versions/{version_id}",
                params={"path": item["path"]},
            )
            if result is not None:
                self.page.close(history_dialog)
                self._notify("Versión eliminada")
                self._show_file_versions(item, editor_dialog)

        def clear_all(e: ft.ControlEvent) -> None:
            def confirm(ev: ft.ControlEvent) -> None:
                result = self._request(
                    "DELETE", "/api/files/versions", params={"path": item["path"]}
                )
                self.page.close(confirm_dialog)
                if result is not None:
                    self.page.close(history_dialog)
                    self._notify("Historial eliminado")
                    self._show_file_versions(item, editor_dialog)

            confirm_dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text("Borrar todo el historial", color=TEXT),
                content=ft.Text(
                    f"Se eliminarán todas las versiones guardadas de {item['name']}. Esta acción no se puede deshacer.",
                    color=MUTED,
                    size=13,
                ),
                actions=[
                    ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(confirm_dialog)),
                    ft.ElevatedButton("Borrar todo", icon=ft.icons.DELETE_OUTLINE, bgcolor=DANGER, color="#FFFFFF", on_click=confirm),
                ],
            )
            self.page.open(confirm_dialog)

        if not versions:
            rows.append(ft.Text("Todavía no hay versiones guardadas.", color=MUTED, size=12))
        for version in versions:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(version["created"]))
            rows.append(
                ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(stamp, color=TEXT, size=12),
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
            result = self._request(
                "POST",
                "/api/files/versions/restore",
                params={"path": item["path"], "version_id": version_id},
            )
            if result is not None:
                self.page.close(history_dialog)
                self.page.close(editor_dialog)
                self._notify("Versión restaurada")
                self.refresh_listing(self.current_path)

        history_dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Versiones · {item['name']}", color=TEXT),
            content=ft.Container(width=440, content=ft.Column(rows, tight=True, spacing=10)),
            actions=[
                ft.TextButton("Borrar todo", icon=ft.icons.DELETE_SWEEP, on_click=clear_all)
                if versions
                else ft.Container(),
                ft.TextButton("Cerrar", on_click=lambda e: self.page.close(history_dialog)),
            ],
        )
        self.page.open(history_dialog)

    def show_rename(self, item: dict) -> None:
        field = self._field("Nuevo nombre", item["name"], autofocus=True)

        def do_rename(e: ft.ControlEvent) -> None:
            name = (field.value or "").strip()
            if not name:
                return
            result = self._request("PUT", "/api/files/rename", json={"path": item["path"], "new_name": name})
            if result is not None:
                self.page.close(dialog)
                self._notify("Elemento renombrado")
                self.refresh_listing(self.current_path)

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Renombrar", color=TEXT),
            content=field,
            actions=[
                ft.TextButton("Cancelar", on_click=lambda e: self.page.close(dialog)),
                ft.ElevatedButton("Renombrar", bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=do_rename),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        field.on_submit = do_rename
        self.page.open(dialog)

    def confirm_delete(self, item: dict) -> None:
        def do_delete(e: ft.ControlEvent) -> None:
            result = self._request("DELETE", "/api/files", params={"path": item["path"]})
            self.page.close(dialog)
            if result is not None:
                self._notify("Elemento eliminado")
                self.refresh_listing(self.current_path)

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Eliminar", color=TEXT),
            content=ft.Text(f"¿Eliminar «{item['name']}» de forma permanente?", color=MUTED, size=13),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda e: self.page.close(dialog)),
                ft.ElevatedButton("Eliminar", bgcolor=DANGER, color="#FFFFFF", on_click=do_delete),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def _new_folder(self, e: ft.ControlEvent) -> None:
        field = self._field("Nombre de la carpeta", autofocus=True)

        def create(ev: ft.ControlEvent) -> None:
            name = (field.value or "").strip()
            if not name:
                return
            result = self._request("POST", "/api/files/mkdir", json={"path": self.current_path, "name": name})
            if result is not None:
                self.page.close(dialog)
                self._notify("Carpeta creada")
                self.refresh_listing(self.current_path)

        field.on_submit = create
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Nueva carpeta", color=TEXT),
            content=field,
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialog)),
                ft.ElevatedButton("Crear", icon=ft.icons.CREATE_NEW_FOLDER, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=create),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def _new_text_file(self, e: ft.ControlEvent) -> None:
        name_field = self._field("Nombre del archivo", "texto", autofocus=True)
        content_field = self._field(
            "Escribe tu texto",
            multiline=True,
            min_lines=10,
            max_lines=18,
            text_style=ft.TextStyle(font_family="monospace", size=13),
            expand=True,
        )

        def save(ev: ft.ControlEvent) -> None:
            name = (name_field.value or "").strip()
            if name.lower().endswith(".txt"):
                name = name[:-4]
            if not name or name in {".", ".."} or any(char in name for char in ("/", "\\", "\x00")):
                self._notify("Escribe un nombre válido, sin rutas ni barras.", True)
                return
            filename = f"{name}.txt"
            listing = self._request("GET", "/api/files", params={"path": self.current_path})
            if listing is None:
                return
            if any(item.get("name", "").casefold() == filename.casefold() for item in listing.get("items", [])):
                self._notify(f"Ya existe «{filename}» en esta carpeta.", True)
                return
            path = f"{self.current_path}/{filename}".lstrip("/")
            result = self._request(
                "PUT",
                "/api/files/write",
                json={"path": path, "content": content_field.value or ""},
            )
            if result is not None:
                self.page.close(dialog)
                self._notify(f"{filename} creado")
                self.refresh_listing(self.current_path)

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Escribir archivo de texto", color=TEXT, size=16),
            content=ft.Container(
                width=640,
                height=390,
                content=ft.Column([name_field, content_field], spacing=14, expand=True),
            ),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialog)),
                ft.ElevatedButton("Crear .txt", icon=ft.icons.SAVE, bgcolor=ACCENT_STRONG, color="#FFFFFF", on_click=save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def _download_zip(self, e: ft.ControlEvent) -> None:
        self._open_url(self._zip_url(self.current_path))

    def _pick_upload(self, e: ft.ControlEvent) -> None:
        if sys.platform.startswith("linux"):
            self._show_file_picker()
            return
        self.file_picker.pick_files(allow_multiple=True, dialog_title="Seleccionar archivos para subir")

    def _on_files_picked(self, e: ft.FilePickerResultEvent) -> None:
        if not e.files:
            return
        paths = [f.path for f in e.files if f.path]
        if paths and not bool(getattr(self.page, "web", False)):
            threading.Thread(target=self._upload_worker, args=(paths,), daemon=True).start()
            return
        if not bool(getattr(self.page, "web", False)):
            self._notify("El selector no devolvió rutas de archivo", True)
            return
        self.web_upload_queue.clear()
        self.web_upload_results.clear()
        self.web_upload_remaining = len(e.files)
        uploads = []
        try:
            for picked in e.files:
                suffix = Path(picked.name).suffix[:20]
                stored_name = f"{uuid.uuid4().hex}{suffix}"
                self.web_upload_queue.append(self.web_upload_dir / stored_name)
                uploads.append(
                    ft.FilePickerUploadFile(
                        name=picked.name,
                        upload_url=self.page.get_upload_url(stored_name, 3600),
                    )
                )
        except Exception as exc:
            self.web_upload_queue.clear()
            self.web_upload_remaining = 0
            self._notify(f"No se pudieron preparar los archivos: {exc}", True)
            return
        self.upload_active = True
        self.upload_done = False
        self.upload_error = None
        self.upload_label = f"Preparando {len(uploads)} archivo(s)…"
        self.upload_speed = "Esperando la transferencia"
        self.upload_progress = 0.0
        self.file_picker.upload(uploads)

    def _on_file_uploaded(self, e: ft.FilePickerUploadEvent) -> None:
        if e.error:
            self.upload_error = f"No se pudo recibir {e.file_name}: {e.error}"
            self.upload_active = False
            self.web_upload_queue.clear()
            self.web_upload_remaining = 0
            return
        progress = e.progress or 0.0
        if progress < 1:
            completed = len(self.web_upload_results)
            self.upload_progress = (completed + progress) / max(completed + self.web_upload_remaining, 1)
            self.upload_label = e.file_name
            self.upload_speed = "Recibiendo desde el navegador…"
            return
        if not self.web_upload_queue:
            return
        self.web_upload_results.append(self.web_upload_queue.popleft())
        self.web_upload_remaining -= 1
        if self.web_upload_remaining <= 0:
            selected = [str(path) for path in self.web_upload_results]
            self.upload_label = "Guardando en la carpeta compartida…"
            self.upload_speed = "Subiendo a Vortex"
            threading.Thread(target=self._upload_worker, args=(selected,), daemon=True).start()

    def _upload_worker(self, paths: list[str]) -> None:
        target_path = self.current_path
        self.upload_target_path = target_path
        self.upload_cancel.clear()
        self.upload_cancellable = False
        self.upload_active = True
        self.upload_done = False
        self.upload_error = None
        for path in paths:
            if self.upload_cancel.is_set():
                break
            self.upload_progress = 0.0
            self.upload_label = Path(path).name
            self.upload_speed = ""
            try:
                self._upload_one(Path(path), target_path)
            except Exception as exc:
                self.upload_error = str(exc)
                break
            finally:
                local = Path(path)
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
        size = local.stat().st_size
        if size <= self.config.direct_upload_limit:
            self.upload_cancellable = False
            self.upload_speed = "Subiendo…"
            with open(local, "rb") as handle:
                files = {"file": (local.name, handle, "application/octet-stream")}
                response = self.http.post(
                    "/api/files/upload",
                    files=files,
                    data={"target_dir": target_dir},
                    headers=self._auth_headers(),
                    timeout=None,
                )
            if response.status_code >= 400:
                raise RuntimeError(self._response_error(response))
            self.upload_progress = 1.0
            return
        self.upload_cancellable = True
        fingerprint = hashlib.sha256()
        with open(local, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                fingerprint.update(block)
        resume_key = hashlib.sha256(
            f"{self.base_url}|{target_dir}|{local.name}|{size}|{fingerprint.hexdigest()}".encode()
        ).hexdigest()
        state_file = app_data_dir() / "resumable_uploads.json"
        try:
            saved_uploads = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved_uploads = {}
        if not isinstance(saved_uploads, dict):
            saved_uploads = {}
        saved = saved_uploads.get(resume_key, {})
        upload_id = saved.get("upload_id")
        status = None
        if upload_id:
            try:
                response = self.http.get(
                    f"/api/files/upload/{upload_id}", headers=self._auth_headers(), timeout=15
                )
                if response.status_code == 200:
                    status = response.json()
                    if (
                        status.get("size") != size
                        or status.get("filename") != local.name
                        or status.get("target_dir") != target_dir
                    ):
                        status = None
            except httpx.HTTPError:
                status = None
        if status is None:
            if upload_id:
                saved_uploads.pop(resume_key, None)
            init = self.http.post(
                "/api/files/upload/init",
                json={"filename": local.name, "size": size, "target_dir": target_dir, "chunk_size": self.config.chunk_size},
                headers=self._auth_headers(),
                timeout=30,
            )
            if init.status_code >= 400:
                raise RuntimeError(self._response_error(init))
            info = init.json()
            upload_id = info["upload_id"]
            status = info
        saved_uploads[resume_key] = {"upload_id": upload_id}
        state_file.parent.mkdir(parents=True, exist_ok=True)
        temp_state = state_file.with_suffix(".tmp")
        temp_state.write_text(json.dumps(saved_uploads), encoding="utf-8")
        temp_state.replace(state_file)
        chunk_size = status["chunk_size"]
        count = status["chunk_count"]
        received = set(status.get("received", []))
        sent = sum(
            min(chunk_size, max(size - index * chunk_size, 0)) for index in received
        )
        self.upload_progress = sent / max(size, 1)
        sent_this_run = 0
        started = time.time()
        with open(local, "rb") as handle:
            for index in range(count):
                if self.upload_cancel.is_set():
                    return
                if index in received:
                    continue
                chunk = handle.read(chunk_size)
                last_error = None
                for _ in range(3):
                    try:
                        response = self.http.put(
                            f"/api/files/upload/{upload_id}/{index}",
                            content=chunk,
                            headers=self._auth_headers(),
                            timeout=None,
                        )
                        if response.status_code < 400:
                            last_error = None
                            break
                        last_error = self._response_error(response)
                    except httpx.HTTPError as exc:
                        last_error = str(exc)
                if last_error:
                    raise RuntimeError(last_error)
                sent += len(chunk)
                sent_this_run += len(chunk)
                self.upload_progress = sent / max(size, 1)
                elapsed = max(time.time() - started, 0.001)
                self.upload_speed = human_speed(sent_this_run / elapsed)
        complete = self.http.post(f"/api/files/upload/{upload_id}/complete", headers=self._auth_headers(), timeout=300)
        if complete.status_code >= 400:
            raise RuntimeError(self._response_error(complete))
        saved_uploads.pop(resume_key, None)
        temp_state.write_text(json.dumps(saved_uploads), encoding="utf-8")
        temp_state.replace(state_file)

    def _response_error(self, response: httpx.Response) -> str:
        try:
            detail = response.json().get("detail", response.text)
            if isinstance(detail, dict):
                return detail.get("message", str(detail))
            return str(detail)
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
            message = self.upload_error
            self.upload_error = None
            self.upload_done = False
            self.upload_widget.visible = False
            self.page.update()
            self._notify(f"Error de subida: {message}", True)
            if self.current_path == self.upload_target_path:
                self.refresh_listing(self.upload_target_path)
            return
        if self.upload_done:
            self.upload_done = False
            self.upload_widget.visible = False
            self.page.update()
            message = (
                "Subida cancelada; puedes reanudarla al seleccionar el mismo archivo."
                if self.upload_cancel.is_set()
                else "Subida completada"
            )
            self.upload_cancel.clear()
            self._notify(message)
            if self.current_path == self.upload_target_path:
                self.refresh_listing(self.upload_target_path)

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
            data = self._request("GET", "/api/files/search", params={"path": self.current_path, "q": query, "limit": 60})
            items = data["items"] if data else []
        else:
            data = self._request("GET", "/api/files", params={"path": self.current_path})
            items = data["items"] if data else []
        self.palette_results.controls.clear()
        if not items:
            self.palette_results.controls.append(ft.Container(padding=20, content=ft.Text("Sin coincidencias", size=12, color=MUTED)))
        for item in items[:60]:
            self.palette_results.controls.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    border_radius=8,
                    on_click=lambda e, it=item: self._palette_open(it),
                    on_hover=self._hover_row,
                    content=ft.Row(
                        [
                            ft.Icon(name=icon_for_kind(item["kind"]), color=color_for_kind(item["kind"]), size=18),
                            ft.Text(item["name"], size=13, color=TEXT, expand=True, overflow=ft.TextOverflow.ELLIPSIS),
                            ft.Text("Carpeta" if item["is_dir"] else human_size(item["size"]), size=11, color=MUTED),
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

    def _open_logs(self, e: ft.ControlEvent) -> None:
        if self.role != "host":
            self._notify("La consola de logs solo está disponible para el host", True)
            return
        self.logs_list = ft.ListView(expand=True, spacing=1, height=440, auto_scroll=True)
        self.logs_count_seen = -1
        self.logs_dialog_open = True
        dialog = ft.AlertDialog(
            modal=False,
            title=ft.Row([ft.Icon(name=ft.icons.TERMINAL, color=ACCENT, size=18), ft.Text("Consola de auditoría en tiempo real", color=TEXT, size=15)]),
            content=ft.Container(width=860, content=self.logs_list),
            actions=[ft.TextButton("Cerrar", on_click=lambda ev: self._close_logs(dialog))],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)
        self._refresh_logs()

    def _close_logs(self, dialog: ft.AlertDialog) -> None:
        self.logs_dialog_open = False
        self.page.close(dialog)

    def _refresh_logs(self) -> None:
        if self.logs_list is None:
            return
        records = list(self.log_buffer)[-200:]
        if len(records) == self.logs_count_seen:
            return
        self.logs_count_seen = len(records)
        self.logs_list.controls.clear()
        colors = {"INFO": MUTED, "WARNING": WARNING, "ERROR": DANGER, "SECURITY": "#F472B6"}
        for record in records:
            severity = record.get("severity", "INFO")
            line = f"{record.get('timestamp', '')}  {severity:<8} {record.get('action', '')}  {record.get('client_ip', '')}  {record.get('target_path', '')}  {record.get('status_code', '')}"
            self.logs_list.controls.append(ft.Text(line, size=10, color=colors.get(severity, MUTED), font_family="monospace", selectable=True))
        try:
            self.page.update()
        except Exception:
            pass

    def _stop_server(self, e: ft.ControlEvent) -> None:
        def do_stop(ev: ft.ControlEvent) -> None:
            self.page.close(dialog)
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

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Detener servidor", color=TEXT),
            content=ft.Text("Se cerrarán las conexiones y el acceso a los archivos quedará suspendido.", color=MUTED, size=13),
            actions=[
                ft.TextButton("Cancelar", on_click=lambda ev: self.page.close(dialog)),
                ft.ElevatedButton("Detener", bgcolor=DANGER, color="#FFFFFF", on_click=do_stop),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.open(dialog)

    def _on_keyboard(self, e: ft.KeyboardEvent) -> None:
        key = (e.key or "").lower()
        if e.ctrl and key == "k":
            self.open_palette()
        elif key == "escape":
            if self.palette_dialog is not None:
                self._close_palette()
        elif e.ctrl and key == "r" and self.http is not None:
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
    target = lambda page: VortexUI(page).start()
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
            target=target,
            view=ft.AppView.WEB_BROWSER,
            upload_dir=str(app_data_dir() / "browser-picker"),
        )
        return
    ft.app(target=target)
