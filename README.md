# VORTEX

Servidor local de archivos tipo NAS ligero, multiplataforma (**Windows, Linux, Web y móvil vía navegador**), con una interfaz centrada en búsqueda y comandos inspirada en *HizSearch*. Diseñado bajo los principios de **Cero Fricción** para el usuario y **Cero Confianza (Zero Trust)** en la red local.

> Este repositorio contiene la implementación completa descrita en `documento/vortex.txt`.

## Características

- **Backend asíncrono** con FastAPI + Uvicorn.
- **UI de escritorio** con Flet (host del servidor y consola de logs en tiempo real).
- **Cliente web/móvil** servido por el propio servidor, con paleta de comandos `Ctrl+K`, vista previa y editor.
- **Búsqueda difusa** con operadores: `type:video`, `ext:pdf`, `size:>1gb`, `size:<10mb`.
- **Subidas inteligentes**: directa para `< 100 MB` y *auto-chunking* de 10 MB para archivos grandes, con reanudación de bloques y widget flotante **Mini-Vortex**.
- **Descargas con `Range Requests`** (seek de vídeo) y **ZIP en streaming** con `zipstream-ng` (sin disco intermedio).
- **Acceso de invitados siempre activo**: cualquier invitado en la LAN obtiene control total sobre los archivos y carpetas compartidos.
- **Autenticación del host**: automática (token), manual (contraseña) o nula (LAN pública).
- **Zero Trust**: protección contra *path traversal*, JWT en memoria, *rate limiting* (5 intentos / 15 min) y sin filtración de existencia de archivos a usuarios no autenticados.
- **Auditoría JSON** con rotación (10 MB × 5) y consola en vivo solo para el host.
- **Anuncio mDNS** para `vortex.local` en la LAN.
- **Comprobación de puertos y permisos** con `psutil`, con *fallback* limpio si se deniega la elevación.
- **Empaquetado** para Windows (`.exe`) y Linux con Flet + PyInstaller.

## Requisitos

- Python **3.10+** (recomendado 3.13).
- Windows 10/11 o Linux (probado en Debian/Ubuntu).
- **Linux (ventana nativa):** el cliente de escritorio de Flet requiere `libmpv.so.1`.
  Instala la biblioteca que la proporcione (en algunas distribuciones es `libmpv1`).
  Si no está disponible, Vortex abre automáticamente la interfaz en el navegador en lugar de fallar.
- Para compilar en Windows, ejecutar los scripts **en Windows** (PyInstaller no genera `.exe` desde Linux).

## Instalación

```bash
cd documento/vortex
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows
# .venv\Scripts\activate

pip install -r requirements.txt
```

## Ejecución

### Escritorio (host con UI Flet)

```bash
python run.py
```

Se abre la ventana de configuración: carpeta raíz, puerto y modo de autenticación del host. Los invitados siempre tienen control total sobre el contenido compartido. Al iniciar, el servidor escucha en toda la LAN y la app entra como host.

La ventana nativa usa un marco propio con la paleta de Vortex: barra de título arrastrable, estado `EN LÍNEA`/`DETENIDO` y controles de minimizar, maximizar y cerrar. El cliente web omite ese marco y usa la página del navegador. En Linux, si falta `libmpv.so.1`, la interfaz se abre automáticamente en el navegador en lugar de fallar.

### Servidor sin interfaz (headless)

```bash
python run.py serve --dir /ruta/a/compartir --port 8080 --auth auto
# o también
python -m vortex serve --dir /ruta/a/compartir --port 8080 --auth manual --password "mi-clave"
```

Opciones: `--auth auto|manual|none`, `--token`, `--password`, `--hostname`.

### Acceso desde otros dispositivos

- Web: `http://vortex.local:8080` (requiere Bonjour en Windows o Avahi en Linux) o `http://IP-DEL-HOST:8080`.
- Móvil: abre la misma URL en el navegador; la interfaz es responsive.
- Escritorio: la app Flet actúa como host y cliente local.
- En Chromebook, si Vortex corre en el entorno Linux, habilita el reenvío TCP del puerto del servidor en
  **Configuración > Desarrolladores > Linux > Reenvío de puertos**. Desde otros dispositivos usa la IP
  Wi-Fi del Chromebook, no la IP interna del contenedor.

## Modos de autenticación

| Modo | Comportamiento |
|---|---|
| `auto` | El host usa un token corto tipo `X7-B9K2`, mostrado en su configuración. |
| `manual` | El host usa una contraseña definida por el usuario, almacenada con `passlib` (PBKDF2-SHA256). |
| `none` | El host no usa credenciales; el acceso sigue limitado a la LAN. |

Los invitados siempre pueden leer, crear, editar, renombrar y borrar archivos y carpetas dentro de la raíz compartida. El token o contraseña protege el acceso administrativo del host; no restringe a los invitados. En modo `none`, el host local (loopback) conserva privilegios de host.

## Compilación

### Windows (primero)

En una máquina Windows:

```bat
build\build_windows.bat
```

Genera `dist\windows\Vortex.exe` con PyInstaller (`build\vortex.spec`) en el entorno `.venv-win`:

```bat
python -m PyInstaller build\vortex.spec --noconfirm --clean --distpath dist\windows --workpath build\work-windows
```

### Linux

```bash
chmod +x build/build_linux.sh
./build/build_linux.sh
```

### Notas de empaquetado

- El *spec* de PyInstaller incluye el cliente de escritorio de Flet y recopila los módulos de
  `uvicorn`, `zeroconf`, `jose`, `passlib`, `zipstream`, `psutil`, `fastapi` y `starlette`.
- Los archivos de `vortex/static` se incluyen como datos y se localizan con
  `vortex.resources.resource_path`, tanto en desarrollo como dentro del ejecutable.
- No ejecutes `flet pack` desde la raíz del repositorio: elimina el directorio `build/` completo,
  que contiene los scripts, iconos y el propio *spec*.
- En el ejecutable de Windows sin consola, `sys.stdout`/`sys.stderr` pueden ser `None`. Vortex los
  redirige a `nul` antes de iniciar uvicorn para evitar el error
  `Unable to configure formatter 'default'`.
- El icono de Windows está en `build/vortex.ico`.

## Estructura

```
vortex/
├── run.py                      # Punto de entrada
├── vortex/
│   ├── config.py               # Configuración persistente y tokens
│   ├── cli.py                  # CLI (escritorio / headless)
│   ├── server.py               # Control del servidor Uvicorn en hilo
│   ├── resources.py            # Rutas de assets (desarrollo y ejecutable)
│   ├── stdstreams.py           # stdout/stderr seguros sin consola
│   ├── backend/
│   │   ├── app.py              # Fábrica FastAPI + SPA
│   │   ├── state.py            # Runtime compartido
│   │   ├── auth.py             # JWT en memoria + passlib
│   │   ├── security.py         # Path traversal, rate limit, búsqueda difusa
│   │   ├── transfer.py         # Chunks, rangos, conflictos, ZIP
│   │   ├── audit.py            # Logs JSON rotativos + listeners
│   │   ├── mdns.py             # Anuncio zeroconf
│   │   ├── system.py           # Puertos, disco, elevación
│   │   └── routers/            # auth, files, system, logs (WS)
│   ├── frontend/               # Interfaz Flet (host)
│   └── static/                 # Cliente web SPA (HTML/CSS/JS)
├── build/                      # Scripts Windows/Linux + spec
└── tests/                      # Pruebas de seguridad y transferencias
```

## API principal

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/api/auth/status` | Estado público de autenticación |
| `POST` | `/api/auth/login` | Login (token/contraseña) |
| `GET` | `/api/auth/me` | Rol actual |
| `GET` | `/api/files?path=` | Listar directorio |
| `GET` | `/api/files/search` | Búsqueda difusa con filtros |
| `GET` | `/api/files/download` | Descarga con `Range` / `?inline=1` |
| `GET` | `/api/files/zip` | ZIP en streaming |
| `GET` | `/api/files/read` | Leer texto para vista previa/editor |
| `PUT` | `/api/files/write` | Guardar texto |
| `POST` | `/api/files/mkdir` | Crear carpeta |
| `PUT` | `/api/files/rename` | Renombrar |
| `DELETE` | `/api/files?path=` | Eliminar |
| `POST` | `/api/files/upload` | Subida directa (multipart) |
| `POST` | `/api/files/upload/init` | Iniciar subida por bloques |
| `PUT` | `/api/files/upload/{id}/{index}` | Enviar bloque |
| `GET` | `/api/files/upload/{id}` | Estado/reanudación |
| `POST` | `/api/files/upload/{id}/complete` | Finalizar |
| `GET` | `/api/system/info` | Disco, plataforma, uptime |
| `GET` | `/api/system/port-check` | Comprobar puerto |
| `WS` | `/api/ws/logs?token=` | Logs en vivo (host) |

## Seguridad

- Toda ruta se resuelve con `pathlib.Path.resolve()` y se valida con `is_relative_to(base_dir)`; si escapa, `403` inmediato y evento `PATH_TRAVERSAL`.
- La autenticación se evalúa **antes** de resolver rutas: nunca se confirma ni niega la existencia de archivos a usuarios no autenticados.
- Los enlaces profundos (`/descargar/...`) los intercepta el cliente web: guarda la ruta, muestra el login y, tras autenticar, descarga. El backend no expone información.
- JWT firmados con secreto efímero y `jti` en memoria; al reiniciar el servidor las sesiones caducan.
- *Rate limiting* por IP: 5 intentos fallidos → bloqueo 15 minutos (eventos `LOGIN_FAILED` y `RATE_LIMIT_BLOCK`).
- Los binarios/scripts subidos nunca se ejecutan; se sirven con `X-Content-Type-Options: nosniff` y extensión forzada a descarga para HTML/SVG/JS.
- Auditoría JSON con los campos `timestamp, event_id, severity, client_ip, user_agent, action, target_path, status_code, details`.

## Pruebas

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Licencia

MIT.
