# AGENTS.md — Vortex

Guía para agentes de IA que trabajen en este repositorio. El proyecto implementa la
especificación de `../vortex.txt`: servidor NAS local (FastAPI) + host de escritorio
(Flet) + cliente web SPA, multiplataforma y Zero Trust en la LAN.

## Entorno y arranque

- Python **3.13** (mínimo 3.10) y `pip`.
- El entorno virtual vive en `.venv` (ignorado por git). No lo recrees si ya existe.
- Instalación: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.
- Escritorio (host): `.venv/bin/python run.py`.
- Headless: `.venv/bin/python run.py serve --dir <ruta> --port 8080 --auth auto --token <token>`.
- La primera ejecución de escritorio descarga el cliente Flet (requiere internet).

## Comandos de verificación (obligatorios antes de terminar)

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q vortex tests
node --check vortex/static/js/app.js   # si node está disponible
.venv/bin/ruff check vortex tests      # opcional si ruff está instalado
```

No hay script `make` ni `npm`; no inventes otros comandos de test/build.

## Mapa de arquitectura

- `vortex/config.py`: `ServerConfig` (dataclass), persistencia en `platformdirs`, tokens.
- `vortex/server.py`: `ServerController` (Uvicorn en hilo para Flet) y `run_headless`.
- `vortex/backend/app.py`: fábrica FastAPI; catch-all SPA para `/browse/...` y `/descargar/...`.
- `vortex/backend/state.py`: `Runtime` compartido (base_dir, auth, uploads, auditoría, mDNS).
- `vortex/backend/deps.py`: dependencias de acceso (`require_read/write/host`).
- `vortex/backend/security.py`: path traversal, búsqueda difusa, filtros, rate limiting.
- `vortex/backend/transfer.py`: subida directa/por bloques, rangos HTTP, conflictos, ZIP.
- `vortex/backend/audit.py`: log JSON rotativo (10 MB × 5) + listeners en vivo.
- `vortex/backend/routers/`: `auth`, `files`, `system`, `logs` (WebSocket).
- `vortex/frontend/app.py`: UI Flet del host (configuración, HizSearch, preview, editor, logs).
- `vortex/static/`: SPA del cliente web (`index.html`, `css/style.css`, `js/app.js`).
- `tests/`: pytest con `TestClient`; `conftest.py` crea el árbol temporal y `auth_headers`.

## Convenciones

- Código e identificadores en **inglés**; textos de UI, mensajes, auditoría y docs en **español**.
- No añadas comentarios al código salvo que aporten una razón no obvia.
- Estilo: líneas ≤ 100, tipado moderno (`str | None`, `from __future__ import annotations`).
- Reutiliza utilidades existentes (`resolve_within`, `relative_to`, `unique_path`,
  `SearchQuery`, `human_size`, `icon_for_kind`) en lugar de duplicar lógica.
- No introduzcas dependencias nuevas sin actualizar `requirements*.txt` y `pyproject.toml`.
- No accedas a la red ni ejecutes binarios subidos: Vortex nunca ejecuta archivos de usuario.

## Invariantes de seguridad (no romper)

- Toda ruta se resuelve con `resolve_within` (`Path.resolve()` + `is_relative_to(base_dir)`).
  Si escapa, responder `403` y auditar `PATH_TRAVERSAL`.
- La autenticación se evalúa **antes** de resolver rutas: nunca filtrar si un archivo existe
  a clientes no autenticados (`401`), incluidos enlaces profundos.
- Rate limiting: 5 fallos por IP → bloqueo 15 min; eventos `LOGIN_FAILED`/`RATE_LIMIT_BLOCK`.
- JWT con secreto efímero y `jti` en memoria; no persistir tokens ni contraseñas.
  `manual_password` nunca se guarda en `config.json` (solo `password_hash`).
- Extensiones activas (`html`, `svg`, `js`, `xml`, etc.) se sirven siempre como adjunto y con
  `X-Content-Type-Options: nosniff`.
- Toda operación sensible (login, CRUD, subidas, descargas, ZIP, elevación, arranque/parada)
  debe registrar auditoría con el esquema: `timestamp, event_id, severity, client_ip,
  user_agent, action, target_path, status_code, details`.

## Transferencias

- Subida directa para `< 100 MB`; por bloques de 10 MB para archivos mayores.
- El backend debe soportar subida de bloques fuera de orden y reanudación vía
  `GET /api/files/upload/{id}`. El ensamblado final se hace en el directorio destino
  (nunca mover entre sistemas de archivos).
- Descargas con `StreamingResponse` y `Range` (206/416). ZIP de carpetas con `zipstream-ng`.
- Conflictos de nombre: renombrado automático `nombre_1.ext`.

## Frontend Flet

- Mantén la paleta de `frontend/theme.py` (`#0B0F17`, `#111827`, `#60A5FA`, `#10B981`).
- Atajo global `Ctrl+K` para HizSearch; `Esc` cierra la paleta.
- La consola de logs en vivo es exclusiva del host (`role == "host"`).
- Al cambiar controles desde hilos, no llames a `page.update()` desde el hilo de trabajo;
  deja que `_poll_loop` refresque el widget (como hace la subida actual).
- En escritorio se oculta la barra nativa y se usa el marco propio `_window_shell`/`_title_bar`:
  marca, estado `EN LÍNEA`/`DETENIDO`, `WindowDragArea` y botones minimizar/maximizar/cerrar.
  `_set_root` reconstruye la barra para reflejar el estado del servidor.
- En modo web (`page.web`) se devuelve el contenido sin marco propio.
- `run_app` comprueba `_libmpv_available()` en Linux; si falta `libmpv.so.1`, avisa y abre
  `ft.app(view=ft.AppView.WEB_BROWSER)` en lugar de fallar. `tests/test_frontend.py` cubre ambos
  caminos con `_StubPage`/`_Window` y `monkeypatch` de `frontend_app.ft.app`.
- `_enable_flet_web_compat` elimina `openapi_prefix` (retirado de FastAPI moderno) antes de
  arrancar el modo web de Flet 0.24; no la quites sin actualizar Flet.

## Empaquetado

- `build/build_windows.bat` (ejecutar **en Windows**) → `dist\windows\Vortex.exe`.
- `build/build_linux.sh` (Linux) → `dist/linux/`.
- `build/vortex.spec` (PyInstaller) es la vía principal en ambas plataformas; el hook de Flet
  incluye el cliente de escritorio. No uses `flet pack` desde la raíz: borra el directorio
  `build/` completo (scripts, iconos y spec).
- Windows usa un entorno aparte `.venv-win` para no chocar con el `.venv` de Linux.
- Incluye siempre `vortex/static` como dato adicional y resuelve rutas con
  `vortex.resources.resource_path` para que funcionen dentro del ejecutable.
- En ejecutables *windowed* `sys.stdout`/`sys.stderr` pueden ser `None`; llama a
  `vortex.stdstreams.ensure_standard_streams()` antes de configurar uvicorn o usar `print`.
  Sin esto, uvicorn falla con `Unable to configure formatter 'default'`.

## Git

- No hagas commit, push ni PR salvo que se pida explícitamente.
- No commitees `.venv`, `dist/`, logs, `config.json` ni secretos/tokens.
- Revisa `git status` y `git diff` antes de cualquier commit.
