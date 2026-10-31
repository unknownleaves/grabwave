# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe: a folder-style (onedir) windowed app. The installer wraps this folder.
import sys
from PyInstaller.utils.hooks import collect_all

datas = [("assets", "assets"), ("THIRD_PARTY.md", ".")]
binaries = []
hiddenimports = ["ui_extra", "updater"]

# Packages that load plugins/data at run time need everything collected. Missing optional ones are skipped.
for pkg in ("yt_dlp", "streamlink", "curl_cffi", "certifi", "websockets", "brotli", "mutagen",
            "Cryptodome", "requests", "urllib3", "yt_dlp_ejs"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Grabwave",
    console=False,          # no black console window
    icon="assets/grabwave.icns" if sys.platform == "darwin" else "assets/grabwave.ico",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Grabwave")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Grabwave.app",
        icon="assets/grabwave.icns",
        bundle_identifier="com.grabwave.app",
        info_plist={
            "CFBundleName": "Grabwave",
            "CFBundleDisplayName": "Grabwave",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
