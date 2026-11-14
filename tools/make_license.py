#!/usr/bin/env python3
"""SELLER-ONLY tool for Grabwave license keys. Keep this and grabwave_private.key on YOUR computer only.
Never upload the private key to GitHub, and never put it in the app.

  python tools/make_license.py init                      one time: makes grabwave_private.key, prints the public key
  python tools/make_license.py key --note "jane@mail"    a lifetime key (default)
  python tools/make_license.py key --days 365 --note ... a yearly subscription key
  python tools/make_license.py check GW1....             tells you what a key is (needs the private key file)

After `init`, paste the printed public key into PUBLIC_KEY_HEX in licensing.py and build a new version.
Every key you make is also written to issued_keys.csv so you know who has what.
"""
import argparse
import csv
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import licensing as L  # noqa: E402

KEYFILE = Path("grabwave_private.key")
LOG = Path("issued_keys.csv")


def load_secret() -> bytes:
    if not KEYFILE.exists():
        sys.exit("No grabwave_private.key here. Run `python tools/make_license.py init` first (in this folder).")
    return bytes.fromhex(KEYFILE.read_text().strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    k = sub.add_parser("key")
    k.add_argument("--days", type=int, default=0, help="length in days for a subscription key (default: lifetime)")
    k.add_argument("--note", default="", help="who it is for, e.g. their email (saved in issued_keys.csv only)")
    c = sub.add_parser("check")
    c.add_argument("token")
    a = ap.parse_args()

    if a.cmd == "init":
        if KEYFILE.exists():
            sys.exit("grabwave_private.key already exists. Not overwriting it (that would invalidate every key you sold).")
        secret = os.urandom(32)
        KEYFILE.write_text(secret.hex())
        try:
            os.chmod(KEYFILE, 0o600)
        except Exception:
            pass
        print("Created grabwave_private.key. KEEP IT SAFE and make a backup copy. Never upload it.\n")
        print("Paste this line into licensing.py, then build a new version:\n")
        print(f'PUBLIC_KEY_HEX = "{L.public_from_secret(secret).hex()}"')
    elif a.cmd == "key":
        secret = load_secret()
        if a.days > 0:
            token = L.make_key(secret, L.TERM, int(time.time() // 86400) + a.days)
        else:
            token = L.make_key(secret, L.LIFETIME, 0)
        info = L.parse_key(token, L.public_from_secret(secret).hex())
        new = not LOG.exists()
        with open(LOG, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["date", "note", "type", "days", "key_id"])
            w.writerow([time.strftime("%Y-%m-%d"), a.note, "term" if a.days else "lifetime", a.days, info.key_id])
        print(token)
    else:
        info = L.parse_key(a.token, L.public_from_secret(load_secret()).hex())
        if info is None:
            sys.exit("Not a valid key for your private key.")
        exp = "never" if info.lifetime else time.strftime("%Y-%m-%d", time.gmtime(info.expires_day * 86400))
        print(f"valid · {'lifetime' if info.lifetime else 'subscription'} · expires: {exp} · id {info.key_id}")


if __name__ == "__main__":
    main()
