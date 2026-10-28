#!/usr/bin/env bash
# Turns dist/Grabwave (PyInstaller folder) into one double-clickable dist/Grabwave-<version>-x86_64.AppImage.
set -e
cd "$(dirname "$0")/../.."
VER=$(python3 -c "import re;print(re.search(r'VERSION = .([0-9.]+)', open('app.py').read()).group(1))")
APPDIR=dist/Grabwave.AppDir
rm -rf "$APPDIR" && mkdir -p "$APPDIR/usr/bin"
cp -r dist/Grabwave/. "$APPDIR/usr/bin/"
mkdir -p "$APPDIR/usr/bin/ffmpeg" && cp ffmpeg/ffmpeg "$APPDIR/usr/bin/ffmpeg/ffmpeg" && chmod +x "$APPDIR/usr/bin/ffmpeg/ffmpeg"
cp assets/grabwave.png "$APPDIR/grabwave.png"
cat > "$APPDIR/grabwave.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Grabwave
Comment=Download videos and record live streams
Exec=Grabwave
Icon=grabwave
Terminal=false
Categories=AudioVideo;Network;
DESK
cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export QTWEBENGINE_CHROMIUM_FLAGS="${QTWEBENGINE_CHROMIUM_FLAGS:---no-sandbox}"
exec "$HERE/usr/bin/Grabwave" "$@"
RUN
chmod +x "$APPDIR/AppRun" "$APPDIR/usr/bin/Grabwave"
TOOL=dist/appimagetool.AppImage
if [ ! -f "$TOOL" ]; then
  curl -L -o "$TOOL" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "dist/Grabwave-$VER-x86_64.AppImage"
echo "Done: dist/Grabwave-$VER-x86_64.AppImage"
