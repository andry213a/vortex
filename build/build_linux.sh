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
echo "Empaquetando con PyInstaller (spec de Vortex)..."
python -m PyInstaller build/vortex.spec --noconfirm --clean --distpath dist/linux --workpath build/work-linux

echo
echo "============================================"
echo " Listo. Ejecutable en dist/linux/"
echo "============================================"
ls -la dist/linux

if command -v dpkg-deb >/dev/null 2>&1; then
  package_root="build/linux-package"
  package_file="dist/linux/vortex_1.0.0_amd64.deb"
  rm -rf "$package_root"
  install -Dm755 dist/linux/Vortex "$package_root/opt/vortex/Vortex"
  install -Dm644 build/vortex.png "$package_root/opt/vortex/vortex.png"
  install -Dm644 /dev/null "$package_root/DEBIAN/control"
  cat > "$package_root/DEBIAN/control" <<'EOF'
Package: vortex
Version: 1.0.0
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Vortex
Depends: libc6
Description: Servidor local de archivos Vortex
 Aplicacion de escritorio para compartir archivos en la red local.
EOF
  install -Dm644 /dev/null "$package_root/usr/share/applications/vortex.desktop"
  cat > "$package_root/usr/share/applications/vortex.desktop" <<'EOF'
[Desktop Entry]
Version=1.0
Type=Application
Name=Vortex
Comment=Servidor local de archivos
Exec=/opt/vortex/Vortex
Icon=/opt/vortex/vortex.png
Terminal=false
Categories=Network;Utility;
EOF
  dpkg-deb -Zgzip -z1 --root-owner-group --build "$package_root" "$package_file"
  echo "Instalador Debian creado en $package_file"
else
  echo "dpkg-deb no esta disponible; se creo solo el ejecutable Linux."
fi
