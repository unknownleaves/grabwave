#!/usr/bin/env bash
# Turns dist/Grabwave.app into dist/Grabwave.dmg (drag-to-Applications disk image).
set -e
cd "$(dirname "$0")/../.."
VER=$(python3 -c "import re;print(re.search(r'VERSION = .([0-9.]+)', open('app.py').read()).group(1))")
APP=dist/Grabwave.app
# bundle ffmpeg inside the app (found at Contents/MacOS/ffmpeg/ffmpeg)
mkdir -p "$APP/Contents/MacOS/ffmpeg"
cp ffmpeg/ffmpeg "$APP/Contents/MacOS/ffmpeg/ffmpeg"
chmod +x "$APP/Contents/MacOS/ffmpeg/ffmpeg"
# ad-hoc signature so Apple Silicon Macs will launch it (not a paid Developer ID signature)
codesign --force --deep --sign - "$APP"
STAGE=dist/dmg_stage
rm -rf "$STAGE" && mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Grabwave" -srcfolder "$STAGE" -ov -format UDZO "dist/Grabwave.dmg"
echo "Done: dist/Grabwave.dmg"
