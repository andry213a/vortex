# Localización de recursos estáticos tanto en desarrollo como empaquetado.

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    relativa = Path(*parts)
    bases: list[Path] = []
    directorio_paquete = getattr(sys, "_MEIPASS", None)
    if directorio_paquete:
        bases.append(Path(directorio_paquete) / "vortex")
    bases.append(PACKAGE_DIR)
    for raiz in bases:
        candidato = raiz / relativa
        if candidato.exists():
            return candidato
    return bases[-1] / relativa


def static_dir() -> Path:
    return resource_path("static")


def static_file(*parts: str) -> Path:
    return resource_path("static", *parts)
