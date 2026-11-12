#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "============================================"
echo " VORTEX - Compilacion para Linux"
echo "============================================"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt

echo
echo "Empaquetando con Flet + PyInstaller..."
if ! flet pack run.py --name Vortex --add-data "vortex/static:vortex/static" --distpath dist/linux; then
  echo "Fallo flet pack. Intentando con el spec de PyInstaller..."
  pyinstaller build/vortex.spec --noconfirm --distpath dist/linux --workpath build/work
fi

echo
echo "============================================"
echo " Listo. Ejecutable en dist/linux/"
echo "============================================"
ls -la dist/linux
