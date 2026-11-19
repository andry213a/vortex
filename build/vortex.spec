# Receta de PyInstaller para empaquetar Vortex en Windows y Linux.
# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

RAIZ = Path(SPECPATH).resolve().parent

# Recursos estáticos y datos que deben viajar dentro del ejecutable.
datos = [(str(RAIZ / "vortex" / "static"), "vortex/static")]
for paquete in ("flet", "flet_desktop"):
    try:
        datos += collect_data_files(paquete)
    except Exception:
        pass

# Algunos módulos se cargan de forma dinámica; los incluimos explícitamente.
importaciones_ocultas = []
for paquete in ("uvicorn", "zeroconf", "jose", "passlib", "zipstream", "psutil", "fastapi", "starlette"):
    try:
        importaciones_ocultas += collect_submodules(paquete)
    except Exception:
        pass

# Analizamos las dependencias reales del punto de entrada.
analisis = Analysis(
    [str(RAIZ / "run.py")],
    pathex=[str(RAIZ)],
    binaries=[],
    datas=datos,
    hiddenimports=importaciones_ocultas,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

paquete_pyz = PYZ(analisis.pure)

# Construimos un único ejecutable sin consola para escritorio.

ejecutable = EXE(
    paquete_pyz,
    analisis.scripts,
    analisis.binaries,
    analisis.datas,
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
    icon=str(RAIZ / "build" / "vortex.ico") if os.name == "nt" else None,
)
