"""Safety net for updates: if a new version can't start, put the previous one back.

Only the standard library is used here, and app.py runs guard() before anything else is imported, so
even a broken new version gets the chance to repair itself. Windows installs only (that is where the
updater runs a silent installer).

How it works
  - updater.install() calls begin_update() just before the new installer starts. It remembers where the
    previous release can be downloaded again (the release page keeps every old version).
  - Each start of Grabwave calls guard(). If the new version has been started three times without ever
    reaching a working window (mark_healthy() was never called), it downloads the previous release,
    installs it silently and starts it.
  - mark_healthy() is called once the window has been up for a moment, or when the app is closed normally.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

MAX_FAILED_STARTS = 3
REPO_RELEASES = "https://github.com/unknownleaves/grabwave/releases/download"


def _state_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) if os.name == "nt" \
        else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Grabwave" / "update_state.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text("utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), "utf-8")
    except Exception:
        pass


def previous_release_url(version: str) -> str:
    return f"{REPO_RELEASES}/v{version}/Grabwave-Setup.exe" if version else ""


def begin_update(from_version: str, to_version: str) -> None:
    """Called right before a silent update starts."""
    _save({"from_version": from_version, "to_version": to_version, "from_url": previous_release_url(from_version),
           "starts": 0, "healthy": False, "rolled_back": False})


def mark_healthy() -> None:
    d = _load()
    if d and not d.get("healthy"):
        d["healthy"] = True
        _save(d)


def guard() -> None:
    """Run first thing at start-up (frozen Windows builds only)."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    d = _load()
    if not d or d.get("healthy") or d.get("rolled_back"):
        return
    d["starts"] = int(d.get("starts", 0)) + 1
    _save(d)
    if d["starts"] <= MAX_FAILED_STARTS or not d.get("from_url"):
        return
    d["rolled_back"] = True  # never try twice
    _save(d)
    try:
        folder = Path(tempfile.gettempdir()) / "grabwave-rollback"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "Grabwave-Setup.exe"
        req = urllib.request.Request(d["from_url"], headers={"User-Agent": "Grabwave-Rollback"})
        with urllib.request.urlopen(req, timeout=120) as r, open(target, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        if target.stat().st_size < 10_000_000:  # not a real installer
            return
        subprocess.Popen([str(target), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                          "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS", "/relaunch=1",
                          f"/LOG={folder / 'rollback.log'}"], close_fds=True,
                         creationflags=0x00000008 | 0x00000200 | 0x08000000)
        os._exit(0)
    except Exception:
        return  # couldn't go back (offline?): start normally and let the user see the problem
