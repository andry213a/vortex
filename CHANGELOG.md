# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-12

### Added

- **Vortex NAS**: ligero servidor local de archivos multiplataforma (Web, Escritorio Flet, móvil vía navegador) con interfaz HizSearch.
- **Backend FastAPI + Uvicorn** asíncrono con arquitectura Zero Trust en la LAN.
- **Autenticación tri-modal**: automática (token), manual (contraseña PBKDF2) y nula (LAN pública).
- **Búsqueda difusa** con operadores `type:`, `ext:`, `size:>,<,=`.
- **Subidas inteligentes**: directa para < 100 MB y auto-chunking de 10 MB con reanudación de bloques y widget Mini-Vortex.
- **Descargas con Range Requests** (seek de vídeo) y ZIP streaming con `zipstream-ng`.
- **Auditoría JSON rotativa** (10 MB × 5) con consola en tiempo real exclusiva para el host.
- **Anuncio mDNS** para `vortex.local` y descubrimiento de servidores Vortex en la LAN.
- **Protección contra path traversal**, JWT en memoria con secreto efímero, rate limiting (5 intentos / 15 min).
- **Empaquetado** para Windows (`.exe` con PyInstaller) y Linux (ejecutable + `.deb`).
- **Historial de versiones** de archivos editados con restore.
- **Sincronización de cambios** entre clientes conectados.

### Changed

- N/A (lanzamiento inicial).