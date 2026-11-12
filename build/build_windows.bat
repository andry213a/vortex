@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo ============================================
echo  VORTEX - Compilacion para Windows
echo ============================================

if not exist ".venv" (
    echo Creando entorno virtual...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

echo.
echo Empaquetando con Flet + PyInstaller...
flet pack run.py --name Vortex --icon build\vortex.ico --add-data "vortex/static;vortex/static" --distpath dist\windows

if errorlevel 1 (
    echo.
    echo Fallo flet pack. Intentando con el spec de PyInstaller...
    pyinstaller build\vortex.spec --noconfirm --distpath dist\windows --workpath build\work
)

echo.
echo ============================================
echo  Listo. Busca Vortex.exe en dist\windows\
echo ============================================
dir /b dist\windows
endlocal
