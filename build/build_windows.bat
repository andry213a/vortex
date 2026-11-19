REM Script de compilación de Vortex.exe en Windows.
@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo ============================================
echo  VORTEX - Compilacion para Windows
echo ============================================

REM Elegimos el intérprete de Python disponible en el sistema.
set "EJECUTABLE_PYTHON=python"
where py >nul 2>nul
if not errorlevel 1 set "EJECUTABLE_PYTHON=py -3"

if not exist ".venv-win" (
    echo Creando entorno virtual de compilacion...
    %EJECUTABLE_PYTHON% -m venv .venv-win
    if errorlevel 1 (
        echo No se encontro Python 3.10+ para crear el entorno.
        exit /b 1
    )
)

REM Activamos el entorno aislado de compilación.
call ".venv-win\Scripts\activate.bat"
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
if errorlevel 1 (
    echo No se pudieron instalar las dependencias.
    exit /b 1
)

echo.
REM Generamos el ejecutable con la receta compartida de PyInstaller.
echo Empaquetando con PyInstaller (spec de Vortex)...
python -m PyInstaller build\vortex.spec --noconfirm --clean --distpath dist\windows --workpath build\work-windows
if errorlevel 1 (
    echo.
    echo Fallo el empaquetado de Windows.
    exit /b 1
)

echo.
echo ============================================
echo  Listo. Busca Vortex.exe en dist\windows\
echo ============================================
dir /b dist\windows
endlocal
