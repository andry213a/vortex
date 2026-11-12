from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    relative = Path(*parts)
    bases: list[Path] = []
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        bases.append(Path(bundle_dir) / "vortex")
    bases.append(PACKAGE_DIR)
    for base in bases:
        candidate = base / relative
        if candidate.exists():
            return candidate
    return bases[-1] / relative


def static_dir() -> Path:
    return resource_path("static")


def static_file(*parts: str) -> Path:
    return resource_path("static", *parts)
