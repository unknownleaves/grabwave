"""Grabwave licensing: a free trial plus license keys that are checked offline. No Qt in here.

How it works
  - Keys are signed with a private key that only you hold (tools/make_license.py). The app carries the
    matching PUBLIC key (PUBLIC_KEY_HEX below), so it can check a key without internet and nobody can
    make a key without your private key.
  - Until PUBLIC_KEY_HEX is filled in, licensing is OFF and the app behaves as before (no trial, no limits).
  - Without a valid key the app is a free trial for TRIAL_DAYS days. After that, new downloads, recordings
    and Watch are paused; the Shelf and the player keep working.
  - A key is either "lifetime" (never expires) or "term" (a subscription period that ends on a date).

Honest limits: this is a speed bump, not a vault. A determined person can always patch a Python app.
The aim is that paying is easier than working around it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---- set these two before you start selling ------------------------------------------------------
PUBLIC_KEY_HEX = ""   # printed by:  python tools/make_license.py init     (empty = licensing off)
BUY_URL = ""          # your checkout page, e.g. "https://yourshop.example/grabwave"
# ----------------------------------------------------------------------------------------------------
TRIAL_DAYS = 14
KEY_PREFIX = "GW1."
DAY = 86400

# ============================== Ed25519 (RFC 8032 reference, standard library only) ==============================
_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _inv(x):
    return pow(x, _p - 2, _p)


def _add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _p, (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C, D = 2 * P[3] * Q[3] * _d % _p, 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zi = _inv(P[2])
    x, y = P[0] * zi % _p, P[1] * zi % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _h(s):
    return int.from_bytes(hashlib.sha512(s).digest(), "little") % _q


def _expand(secret):
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_from_secret(secret: bytes) -> bytes:
    return _compress(_mul(_expand(secret)[0], _G))


def sign(secret: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(secret)
    A = _compress(_mul(a, _G))
    r = _h(prefix + msg)
    Rs = _compress(_mul(r, _G))
    s = (r + _h(Rs + A + msg) * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    if len(public) != 32 or len(sig) != 64:
        return False
    A, R = _decompress(public), _decompress(sig[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _q:
        return False
    return _equal(_mul(s, _G), _add(R, _mul(_h(sig[:32] + public + msg), A)))


# ======================================= keys =======================================
LIFETIME, TERM = 1, 2
_PAYLOAD = ">BBI6s"  # version, kind, expiry (days since 1970, 0 = never), key id


def make_key(secret: bytes, kind: int = LIFETIME, expires_day: int = 0, key_id: bytes | None = None) -> str:
    key_id = key_id or os.urandom(6)
    payload = struct.pack(_PAYLOAD, 1, kind, expires_day, key_id)
    return KEY_PREFIX + base64.urlsafe_b64encode(payload + sign(secret, payload)).decode().rstrip("=")


@dataclass
class KeyInfo:
    kind: int
    expires_day: int
    key_id: str

    @property
    def lifetime(self) -> bool:
        return self.kind == LIFETIME or self.expires_day == 0


def parse_key(token: str, public_hex: str | None = None) -> KeyInfo | None:
    """The key's details if the signature is genuine, else None. Expiry is judged separately."""
    public_hex = PUBLIC_KEY_HEX if public_hex is None else public_hex
    try:
        t = "".join((token or "").split())
        if not t.startswith(KEY_PREFIX):
            return None
        raw = base64.urlsafe_b64decode(t[len(KEY_PREFIX):] + "=" * (-len(t[len(KEY_PREFIX):]) % 4))
        size = struct.calcsize(_PAYLOAD)
        payload, sig = raw[:size], raw[size:]
        if len(sig) != 64 or not verify(bytes.fromhex(public_hex), payload, sig):
            return None
        ver, kind, exp, kid = struct.unpack(_PAYLOAD, payload)
        return KeyInfo(kind, exp, kid.hex()) if ver == 1 else None
    except Exception:
        return None


# ======================================= trial and status =======================================
def _dir() -> Path:
    override = os.environ.get("GRABWAVE_LICENSE_DIR")
    if override:
        return Path(override)
    base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) if os.name == "nt" \
        else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Grabwave"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {}


def _write_json(p: Path, d: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), "utf-8")
    except Exception:
        pass


def _second_spot() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) if os.name == "nt" \
        else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    if os.environ.get("GRABWAVE_LICENSE_DIR"):
        return Path(os.environ["GRABWAVE_LICENSE_DIR"]) / "second" / ".t"
    return base / "Grabwave-cache" / ".t"


def _trial_marks(now: float) -> tuple[float, float]:
    """(start, last_seen). Kept in two places so deleting one file doesn't restart the trial."""
    a, b = _read_json(_dir() / "trial.json"), _read_json(_second_spot())
    starts = [x["start"] for x in (a, b) if isinstance(x.get("start"), (int, float))]
    seens = [x["seen"] for x in (a, b) if isinstance(x.get("seen"), (int, float))]
    start = min(starts) if starts else now
    seen = max(seens) if seens else now
    return start, seen


def _save_trial(start: float, seen: float) -> None:
    d = {"start": start, "seen": seen}
    _write_json(_dir() / "trial.json", d)
    _write_json(_second_spot(), d)


@dataclass
class Status:
    state: str          # "off" | "trial" | "active" | "expired"
    days_left: int = 0
    lifetime: bool = False
    message: str = ""

    @property
    def allowed(self) -> bool:
        return self.state in ("off", "trial", "active")


def enabled() -> bool:
    return bool(PUBLIC_KEY_HEX)


def saved_key() -> str:
    return _read_json(_dir() / "license.json").get("key", "")


def activate(token: str) -> tuple[bool, str]:
    """Check a typed-in key and keep it. Returns (ok, message for the person)."""
    info = parse_key(token)
    if info is None:
        return False, "That key isn't valid. Check that you copied all of it, with nothing missing."
    if not info.lifetime and info.expires_day * DAY < time.time():
        return False, "That key has already expired."
    _write_json(_dir() / "license.json", {"key": "".join(token.split())})
    return True, "Thank you! Grabwave is activated." if info.lifetime else "Thank you! Your subscription is active."


def deactivate() -> None:
    try:
        (_dir() / "license.json").unlink()
    except Exception:
        pass


def status(now: float | None = None) -> Status:
    if not enabled():
        return Status("off")
    now = time.time() if now is None else now
    start, seen = _trial_marks(now)
    tampered = now < seen - DAY  # the clock was wound back by more than a day
    if not tampered:
        _save_trial(start, max(seen, now))
    key = saved_key()
    if key:
        info = parse_key(key)
        if info is not None:
            if info.lifetime:
                return Status("active", lifetime=True, message="Licensed. Thank you for supporting Grabwave.")
            left = int((info.expires_day * DAY - now) // DAY) + 1
            if left > 0 and not tampered:
                return Status("active", days_left=left, message=f"Subscription active, {left} day(s) left.")
            return Status("expired", message="Your subscription has ended. Renew to keep downloading.")
    if tampered:
        return Status("expired", message="The computer's clock looks wrong. Set the correct date and try again.")
    left = TRIAL_DAYS - int((now - start) // DAY)
    if left > 0:
        return Status("trial", days_left=left, message=f"Free trial: {left} day(s) left.")
    return Status("expired", message="Your free trial has ended.")
