# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "vortex" / "static"), "vortex/static")]
for package in ("flet", "flet_desktop"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

hiddenimports = []
for package in ("uvicorn", "zeroconf", "jose", "passlib", "zipstream", "psutil", "fastapi", "starlette"):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Vortex",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "build" / "vortex.ico") if os.name == "nt" else None,
)
