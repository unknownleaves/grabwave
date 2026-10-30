"""Finds, downloads and installs newer versions of Grabwave. No Qt in here (the window is in ui_extra.py).

How it works: the release build publishes a small file, latest.json, next to the installers. It lists the
newest version, what's new, and for each system the download address and its SHA-256 fingerprint. Grabwave
fetches that file, and if the version is newer it can download the installer, check the fingerprint and
install it over the running copy, then start the new version.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

# Where the release build publishes latest.json. Change this one line (or set "update_url" in the settings file)
# when downloads move to your own website.
DEFAULT_MANIFEST_URL = "https://github.com/unknownleaves/grabwave/releases/latest/download/latest.json"
UA = "Grabwave-Updater"


class UpdateError(Exception):
    pass


class Cancelled(Exception):
    pass


def parse_version(v: str) -> tuple:
    out = []
    for part in str(v).strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_newer(new: str, current: str) -> bool:
    a, b = parse_version(new), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def platform_key() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def can_self_install() -> bool:
    """Updates install themselves only in the installed app, not when running from source."""
    return bool(getattr(sys, "frozen", False))


def _opener(proxy: str = ""):
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def fetch_manifest(url: str = "", proxy: str = "", timeout: int = 12) -> dict:
    url = url or DEFAULT_MANIFEST_URL
    if not url.lower().startswith("https://"):
        raise UpdateError("The update address must start with https://")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    try:
        with _opener(proxy).open(req, timeout=timeout) as r:
            data = json.loads(r.read(1_000_000).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Couldn't reach the update server ({e})") from e
    if not isinstance(data, dict) or "version" not in data:
        raise UpdateError("The update information is not in the expected format")
    return data


def check(current: str, url: str = "", proxy: str = "") -> dict | None:
    """Returns the update for this computer ({'version','notes','url','sha256','size'}) or None if up to date."""
    m = fetch_manifest(url, proxy)
    if not is_newer(str(m["version"]), current):
        return None
    entry = m.get(platform_key())
    if not isinstance(entry, dict) or not str(entry.get("url", "")).lower().startswith("https://"):
        return None  # a newer version exists but not for this system (yet)
    return {"version": str(m["version"]), "notes": str(m.get("notes", "")).strip(), "url": entry["url"],
            "sha256": str(entry.get("sha256", "")).lower(), "size": int(entry.get("size") or 0)}


def download(entry: dict, progress=None, cancel: threading.Event | None = None, proxy: str = "") -> Path:
    """Download the installer to a temporary folder and verify its fingerprint."""
    folder = Path(tempfile.gettempdir()) / "grabwave-update"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    name = entry["url"].split("?")[0].rsplit("/", 1)[-1] or "update"
    dest = folder / name
    req = urllib.request.Request(entry["url"], headers={"User-Agent": UA})
    h = hashlib.sha256()
    try:
        with _opener(proxy).open(req, timeout=30) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or entry.get("size") or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except Cancelled:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError(f"Download failed ({e})") from e
    want = entry.get("sha256") or ""
    if want and h.hexdigest().lower() != want:
        shutil.rmtree(folder, ignore_errors=True)
        raise UpdateError("The downloaded file didn't match its fingerprint, so it was discarded. "
                          "Please try again.")
    return dest


def install(path: Path) -> bool:
    """Start the installation. Returns True when Grabwave must close now so the new version can take over."""
    key = platform_key()
    if key == "windows":
        # per-user installer: runs without a prompt, closes Grabwave if needed, and starts the new version
        subprocess.Popen([str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                          "/CLOSEAPPLICATIONS", "/relaunch=1"], close_fds=True)
        return True
    if key == "mac":
        exe = Path(sys.executable).resolve()
        app = next((p for p in exe.parents if p.suffix == ".app"), None)
        if app is None or not os.access(app.parent, os.W_OK):
            subprocess.Popen(["open", str(path)])  # can't replace in place: open the disk image for the user
            return False
        script = Path(tempfile.gettempdir()) / "grabwave-update.sh"
        script.write_text(
            '#!/bin/bash\nsleep 2\nMNT=$(mktemp -d)\n'
            'hdiutil attach -nobrowse -quiet -mountpoint "$MNT" "$1" || exit 1\n'
            'if [ -d "$MNT/Grabwave.app" ]; then rm -rf "$2" && cp -R "$MNT/Grabwave.app" "$2" '
            '&& xattr -dr com.apple.quarantine "$2" 2>/dev/null; fi\n'
            'hdiutil detach -quiet "$MNT"\nopen "$2"\n', "utf-8")
        script.chmod(0o755)
        subprocess.Popen(["/bin/bash", str(script), str(path), str(app)], start_new_session=True, close_fds=True)
        return True
    # Linux: replace the AppImage file itself
    target = os.environ.get("APPIMAGE", "")
    if not target or not os.path.isfile(target):
        subprocess.Popen(["xdg-open", str(path.parent)])
        return False
    staged = target + ".new"
    shutil.copyfile(path, staged)
    os.chmod(staged, os.stat(staged).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(staged, target)
    subprocess.Popen(["sh", "-c", f'sleep 2; exec "{target}"'], start_new_session=True, close_fds=True)
    return True
