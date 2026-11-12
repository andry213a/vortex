from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import AUTH_MODES, ServerConfig, generate_auto_token


def _serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vortex serve", description="Ejecutar el servidor Vortex sin interfaz de escritorio")
    parser.add_argument("--dir", required=True, help="Carpeta raíz que se compartirá")
    parser.add_argument("--port", type=int, default=8080, help="Puerto HTTP (1-65535)")
    parser.add_argument("--auth", choices=AUTH_MODES, default="auto", help="Modo de autenticación")
    parser.add_argument("--token", default="", help="Token para el modo automático")
    parser.add_argument("--password", default="", help="Contraseña para el modo manual")
    parser.add_argument("--hostname", default="vortex.local", help="Nombre mDNS")
    parser.add_argument("--guest-write", action="store_true", help="Los invitados pueden escribir")
    parser.add_argument("--allow-guest", action="store_true", help="Permitir acceso sin credenciales")
    return parser


def _config_from_args(args: argparse.Namespace) -> ServerConfig:
    base = Path(args.dir).expanduser()
    if not base.exists() or not base.is_dir():
        raise SystemExit(f"La carpeta no existe: {base}")
    config = ServerConfig(
        base_dir=str(base.resolve()),
        port=args.port,
        auth_mode=args.auth,
        auto_token=args.token or generate_auto_token(),
        hostname=args.hostname,
        guest_write=bool(args.guest_write),
        allow_guest=bool(args.allow_guest),
    )
    if args.auth == "manual":
        if not args.password:
            raise SystemExit("El modo manual requiere --password")
        config.manual_password = args.password
    try:
        config.validate()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return config


def _run_serve(args: argparse.Namespace) -> None:
    from .server import run_headless

    config = _config_from_args(args)
    print(f"Vortex:       http://0.0.0.0:{config.port}")
    print(f"Carpeta:      {config.resolved_base_dir()}")
    if config.auth_mode == "auto":
        print(f"Token:        {config.auto_token}")
    elif config.auth_mode == "manual":
        print("Autenticación: contraseña manual")
    else:
        print("Autenticación: desactivada (acceso público en la LAN)")
    print("Pulsa Ctrl+C para detener el servidor.", flush=True)
    run_headless(config)


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"serve", "headless"}:
        args = _serve_parser().parse_args(argv[1:])
        _run_serve(args)
        return
    parser = argparse.ArgumentParser(prog="vortex", description="Vortex: servidor NAS local")
    parser.add_argument("--headless", action="store_true", help="Ejecutar solo el servidor")
    parser.add_argument("--dir", default="", help="Carpeta raíz")
    parser.add_argument("--port", type=int, default=8080, help="Puerto HTTP")
    parser.add_argument("--auth", choices=AUTH_MODES, default="auto", help="Modo de autenticación")
    parser.add_argument("--token", default="", help="Token del modo automático")
    parser.add_argument("--password", default="", help="Contraseña del modo manual")
    parser.add_argument("--hostname", default="vortex.local", help="Nombre mDNS")
    parser.add_argument("--guest-write", action="store_true")
    parser.add_argument("--allow-guest", action="store_true")
    args = parser.parse_args(argv)
    if args.headless:
        if not args.dir:
            parser.error("--dir es obligatorio en modo headless")
        _run_serve(args)
        return
    from .frontend.app import run_app

    run_app()


if __name__ == "__main__":
    main()
