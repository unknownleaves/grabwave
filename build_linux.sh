#!/usr/bin/env bash
# Build the Linux app -> dist/Grabwave-<version>-x86_64.AppImage  (one file: make executable, double-click)
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt imageio-ffmpeg
mkdir -p ffmpeg
[ -f ffmpeg/ffmpeg ] || python3 -c "import shutil,imageio_ffmpeg;shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(),'ffmpeg/ffmpeg')"
pyinstaller --noconfirm --clean Grabwave.spec
packaging/linux/make_appimage.sh
