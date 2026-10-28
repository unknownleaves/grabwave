"""Direct stream resolvers for a few live-cam sites (public rooms only).

yt-dlp's extractors for these sites break often, so the recorder asks the site's
own web API for the HLS playlist and hands it to streamlink's HLS reader.

Requests are made with a Chrome-like TLS fingerprint (curl_cffi, if installed) and,
optionally, the cookies from the user's own browser, so the site sees the same
session the user already has. Nothing here solves challenges or decodes protected
streams: if the site refuses, the error is reported as-is.
"""
from __future__ import annotations

import uuid
from urllib.parse import urlparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class CamOffline(Exception):
    """Room exists but is offline / private / not public right now."""


def detect(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if "chaturbate." in host:
        return "chaturbate"
    if "stripchat." in host:
        return "stripchat"
    return None


def username(url: str) -> str:
    parts = [s for s in urlparse(url).path.split("/") if s]
    return parts[-1] if parts else ""


class Http:
    """Small wrapper: curl_cffi (Chrome fingerprint) when available, else streamlink's session."""

    def __init__(self, sl, browser: str = "", domain: str = "", cookie_file: str = ""):
        self.sl = sl
        self.cc = None
        try:
            from curl_cffi import requests as cr
            self.cc = cr.Session(impersonate="chrome")
        except Exception:
            pass
        if cookie_file:
            try:
                import http.cookiejar as cj
                jar = cj.MozillaCookieJar(cookie_file)
                jar.load(ignore_discard=True, ignore_expires=True)
                self._add_jar(jar, domain)
            except Exception:
                pass
        if browser:
            self._load_browser_cookies(browser, domain)

    def _add_jar(self, jar, domain: str) -> None:
        for c in jar:
            if domain and domain not in (c.domain or ""):
                continue
            if self.cc is not None:
                self.cc.cookies.set(c.name, c.value, domain=c.domain)
            self.sl.http.cookies.set_cookie(c)

    def _load_browser_cookies(self, browser: str, domain: str) -> None:
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(browser)
        except Exception as e:
            raise RuntimeError(
                f"Could not read cookies from {browser}: {e}. Close the browser and try again "
                f"(Firefox works most reliably; recent Chrome/Edge on Windows may block it).")
        self._add_jar(jar, domain)

    def request(self, method: str, url: str, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        if self.cc is not None:
            headers.pop("User-Agent", None)  # let the impersonated browser UA stand
            return getattr(self.cc, method)(url, headers=headers, timeout=25, **kw)
        headers.setdefault("User-Agent", UA)
        return getattr(self.sl.http, method)(url, headers=headers, raise_for_status=False, **kw)

    def get(self, url, **kw):
        return self.request("get", url, **kw)

    def post(self, url, **kw):
        return self.request("post", url, **kw)


def _get_json(resp, what: str) -> dict:
    if resp.status_code != 200:
        hint = " (blocked by the site / Cloudflare: try Settings > 'Use cookies from browser')" if resp.status_code in (403, 429) else ""
        raise RuntimeError(f"{what}: HTTP {resp.status_code}{hint}")
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"{what}: response was not JSON (probably a challenge page): {resp.text[:160]!r}")


def _chaturbate(http, url: str) -> list[str]:
    user = username(url)
    tok = uuid.uuid4().hex.upper()[:32]
    r = http.post(
        "https://chaturbate.com/get_edge_hls_url_ajax/",
        data={"room_slug": user, "bandwidth": "high"},
        headers={"X-CSRFToken": tok, "X-Requested-With": "XMLHttpRequest",
                 "Referer": f"https://chaturbate.com/{user}/"},
        cookies={"csrftoken": tok},
    )
    d = _get_json(r, "Chaturbate API")
    status = str(d.get("room_status", "unknown"))
    if d.get("success") and status == "public" and d.get("url"):
        return [d["url"]]
    if not d.get("url"):
        raise CamOffline(status)
    raise RuntimeError(f"Chaturbate: unexpected API response {str(d)[:300]}")


def _stripchat(http, url: str) -> list[str]:
    user = username(url)
    r = http.get(
        f"https://stripchat.com/api/front/v2/models/username/{user}/cam",
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": url},
    )
    d = _get_json(r, "Stripchat API")
    try:
        cam = d["cam"]
        u = d["user"]["user"]
    except Exception:
        raise CamOffline(str(d.get("message") or d.get("error") or "no cam data")[:80])
    name = cam.get("streamName")
    if not (u.get("isLive") and u.get("status") == "public" and name):
        raise CamOffline(str(u.get("status", "offline")))
    srv = (cam.get("viewServers") or {}).get("flashphoner-hls", "")
    cands = [
        f"https://edge-hls.doppiocdn.com/hls/{name}/master/{name}_auto.m3u8",
        f"https://edge-hls.doppiocdn.org/hls/{name}/master/{name}_auto.m3u8",
    ]
    if srv:
        cands += [
            f"https://b-{srv}.doppiocdn.com/hls/{name}/master/{name}_auto.m3u8",
            f"https://b-{srv}.strpst.com/hls/{name}/master_{name}.m3u8",
            f"https://b-{srv}.strpst.com/hls/{name}/{name}.m3u8",
        ]
    return cands


def _pick(streams: dict, quality: str):
    import re
    if quality in streams:
        return streams[quality]

    def res(n):
        m = re.match(r"(\d+)p(\d+)?", n)
        return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)

    ordered = sorted(streams, key=res)
    return streams[ordered[0] if quality == "worst" else ordered[-1]]


def open_stream(sl, url: str, quality: str = "best", browser: str = "", cookie_file: str = ""):
    """Return a streamlink Stream for a public room, or raise CamOffline / RuntimeError."""
    from streamlink.stream.hls import HLSStream

    site = detect(url)
    http = Http(sl, browser, site or "", cookie_file)
    cands = _chaturbate(http, url) if site == "chaturbate" else _stripchat(http, url)
    headers = {"Referer": url, "User-Agent": UA}
    sl.set_option("http-headers", headers)
    last = None
    for c in cands:
        try:
            streams = HLSStream.parse_variant_playlist(sl, c, headers=headers)
            if streams:
                return _pick(dict(streams), quality)
            return HLSStream(sl, c, headers=headers)
        except Exception as e:
            last = e
    raise RuntimeError(f"Could not open any playlist ({site}); last error: {last}")
