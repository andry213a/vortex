# Interfaz de línea de comandos para el modo servidor sin ventana.

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import AUTH_MODES, ServerConfig, generate_auto_token
from .stdstreams import ensure_standard_streams


# Comandos de línea para arrancar el servidor sin ventana.
def _serve_parser() -> argparse.ArgumentParser:
    analizador = argparse.ArgumentParser(prog="vortex serve", description="Ejecutar el servidor Vortex sin interfaz de escritorio")
    analizador.add_argument("--dir", required=True, help="Carpeta raíz que se compartirá")
    analizador.add_argument("--port", type=int, default=8080, help="Puerto HTTP (1-65535)")
    analizador.add_argument("--auth", choices=AUTH_MODES, default="auto", help="Modo de autenticación")
    analizador.add_argument("--token", default="", help="Token para el modo automático")
    analizador.add_argument("--password", default="", help="Contraseña para el modo manual")
    analizador.add_argument("--hostname", default="vortex.local", help="Nombre mDNS")
    return analizador


def _config_from_args(args: argparse.Namespace) -> ServerConfig:
    raiz = Path(args.dir).expanduser()
    if not raiz.exists() or not raiz.is_dir():
        raise SystemExit(f"La carpeta no existe: {raiz}")
    configuracion = ServerConfig(
        base_dir=str(raiz.resolve()),
        port=args.port,
        auth_mode=args.auth,
        auto_token=args.token or generate_auto_token(),
        hostname=args.hostname,
    )
    if args.auth == "manual":
        if not args.password:
            raise SystemExit("El modo manual requiere --password")
        configuracion.manual_password = args.password
    try:
        configuracion.validate()
    except ValueError as excepcion:
        raise SystemExit(str(excepcion)) from excepcion
    return configuracion


def _run_serve(args: argparse.Namespace) -> None:
    from .server import run_headless

    configuracion = _config_from_args(args)
    print(f"Vortex:       http://0.0.0.0:{configuracion.port}")
    print(f"Carpeta:      {configuracion.resolved_base_dir()}")
    if configuracion.auth_mode == "auto":
        print(f"Token:        {configuracion.auto_token}")
    elif configuracion.auth_mode == "manual":
        print("Autenticación: contraseña manual")
    else:
        print("Autenticación: desactivada (acceso público en la LAN)")
    print("Pulsa Ctrl+C para detener el servidor.", flush=True)
    run_headless(configuracion)


# Punto de entrada: modo escritorio o servidor headless.
def main(argv: list[str] | None = None) -> None:
    ensure_standard_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"serve", "headless"}:
        argumentos = _serve_parser().parse_args(argv[1:])
        _run_serve(argumentos)
        return
    analizador = argparse.ArgumentParser(prog="vortex", description="Vortex: servidor NAS local")
    analizador.add_argument("--headless", action="store_true", help="Ejecutar solo el servidor")
    analizador.add_argument("--dir", default="", help="Carpeta raíz")
    analizador.add_argument("--port", type=int, default=8080, help="Puerto HTTP")
    analizador.add_argument("--auth", choices=AUTH_MODES, default="auto", help="Modo de autenticación")
    analizador.add_argument("--token", default="", help="Token del modo automático")
    analizador.add_argument("--password", default="", help="Contraseña del modo manual")
    analizador.add_argument("--hostname", default="vortex.local", help="Nombre mDNS")
    argumentos = analizador.parse_args(argv)
    if argumentos.headless:
        if not argumentos.dir:
            analizador.error("--dir es obligatorio en modo headless")
        _run_serve(argumentos)
        return
    from .frontend.app import run_app

    run_app()


if __name__ == "__main__":
    main()
