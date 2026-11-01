# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe: a folder-style (onedir) windowed app. The installer wraps this folder.
import sys
from PyInstaller.utils.hooks import collect_all

datas = [("assets", "assets"), ("THIRD_PARTY.md", ".")]
binaries = []
hiddenimports = ["engine", "ui_extra", "updater", "rollback", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"]

# Packages that load plugins/data at run time need everything collected. Missing optional ones are skipped.
for pkg in ("yt_dlp", "streamlink", "curl_cffi", "certifi", "websockets", "brotli", "mutagen",
            "Cryptodome", "requests", "urllib3", "yt_dlp_ejs", "gallery_dl"):
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
# Windows only: put a company name, product name and version into the program's Properties tab
version_file = None
if sys.platform == "win32":
    try:
        import re as _re
        from pathlib import Path as _P
        ver = _re.search(r'VERSION = "([0-9.]+)"', _P("app.py").read_text("utf-8")).group(1)
        nums = (tuple(int(x) for x in ver.split(".")) + (0, 0, 0, 0))[:4]
        from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                         VarFileInfo, VarStruct, VSVersionInfo)
        vi = VSVersionInfo(
            ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                              subtype=0x0, date=(0, 0)),
            kids=[StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "Grabwave Software"),
                StringStruct("FileDescription", "Grabwave"),
                StringStruct("FileVersion", ver),
                StringStruct("InternalName", "Grabwave"),
                StringStruct("LegalCopyright", "Copyright (c) 2026 Grabwave Software"),
                StringStruct("OriginalFilename", "Grabwave.exe"),
                StringStruct("ProductName", "Grabwave"),
                StringStruct("ProductVersion", ver)])]),
                  VarFileInfo([VarStruct("Translation", [1033, 1200])])])
        _P("build").mkdir(exist_ok=True)
        version_file = str(_P("build") / "version_info.txt")
        _P(version_file).write_text(str(vi), "utf-8")
    except Exception:
        version_file = None

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
    version=version_file,
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
