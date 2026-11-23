"""Self-test: runs without network or a window. Usage: python tests/run.py   (exit code 1 on any failure)."""
import json, os, sys, tempfile, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_tmp = tempfile.mkdtemp()
os.environ["APPDATA"] = os.environ["XDG_CONFIG_HOME"] = os.environ["GRABWAVE_LICENSE_DIR"] = _tmp
os.environ["HOME"] = _tmp
import engine as E

FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)

# --- which addresses count as lists --------------------------------------------------------
K = E.collection_kind
check("erome album", K("https://www.erome.com/a/AbC") == "erome-album")
check("erome profile", K("https://www.erome.com/someone") == "erome-profile")
check("erome reserved page", K("https://www.erome.com/explore") == "")
check("redgifs user", K("https://www.redgifs.com/users/bob") == "redgifs-user")
check("redgifs single is not a list", K("https://www.redgifs.com/watch/x") == "")
check("listing pornhub model", K("https://www.pornhub.com/model/jane") == "listing:pornhub")
check("single view page", K("https://www.pornhub.com/view_video.php?viewkey=ph1") == "")
check("xhamster user", K("https://xhamster.com/users/bob") == "listing:xhamster")
check("live-cam host is not xhamster list", K("https://xhamsterlive.com/room") == "")
check("eporner profile", K("https://www.eporner.com/profile/zed/") == "listing:eporner")
check("redtube pornstar", K("https://www.redtube.com/pornstar/jane") == "listing:redtube")
check("generic /videos/models/NAME", K("https://bodgirls.com/videos/models/flirtygem/") == "listing:generic")
check("generic single video page", K("https://bodgirls.com/videos/some-video-1/") == "")
check("youtube channel untouched", K("https://www.youtube.com/channel/UCx") == "")
check("any-site /users/NAME", K("https://spankbang.com/playlist/12") == "listing:any")

# --- page readers on made-up pages ---------------------------------------------------------
album = ('<h1>My &amp; Album</h1><video poster="/p.jpg"><source src="https://s1.x/a_480.mp4" res="480">'
         '<source src="https://s1.x/a_720.mp4" res="720"></video><video><source src="https://s1.x/b.mp4"></video>')
E._page_html = lambda url, referer="", headers=None: album
t, ents = E._erome_album("https://www.erome.com/a/Q")
check("album: title decoded", t == "My & Album", t)
check("album: best quality, 2 videos", [e["url"] for e in ents] == ["https://s1.x/a_720.mp4", "https://s1.x/b.mp4"])

def pg(n, nxt=""):
    v = "".join(f'<a href="/video/c{n}-{i}"><img src="t.jpg"></a>' for i in range(20))
    return '<a href="/about">About</a>' + v + (f'<a class="next" href="{nxt}">Next</a>' if nxt else "")
E._page_html = lambda url, referer="", headers=None: pg(int(url.split("pg=")[1]) if "pg=" in url else 1,
                                                          f"/u/bob?pg={(int(url.split('pg=')[1]) if 'pg=' in url else 1)+1}" if ("pg=" not in url or int(url.split("pg=")[1]) < 3) else "")
E._ytdlp_flat = lambda *a: (_ for _ in ()).throw(RuntimeError("no"))
E._gdl_entries = lambda *a: ("", [])
t, ents = E.expand_collection("https://xfree.com/u/bob", E.Config())
check("generic reader follows Next links (3 pages x 20)", len(ents) == 60, len(ents))
single = '<meta property="og:type" content="video.other">' + pg(1)
check("single-video page is not expanded", E._generic_links(single, "https://xfree.com/video/z") == [])

def api(url, referer="", headers=None):
    if "auth/temporary" in url: return json.dumps({"token": "t"})
    n = int(url.split("page=")[1])
    return json.dumps({"pages": 2, "gifs": [{"id": f"g{n}{i}", "urls": {"hd": f"https://m/g{n}{i}.mp4"}} for i in range(2)]})
E._page_html = api
t, ents = E._redgifs_api("https://www.redgifs.com/users/bob", E.Config())
check("redgifs api: both pages", len(ents) == 4 and ents[0]["alt_url"].endswith(".mp4"))

# album-style sites: an album page plays its videos itself; a profile lists albums
_alb = '<h1>Trip | Site</h1><video><source src="https://cdn/1.mp4" res="720"></video><video><source src="https://cdn/2.mp4"></video>'
E._page_html = lambda url, referer="", headers=None: _alb if "/album/" in url else "".join(f'<a href="/album/a{i}"><img src="t.jpg"></a>' for i in range(4))
t_, e_ = E.expand_collection("https://balbums.st/album/Z", E.Config())
check("album page: its own videos", [x["url"] for x in e_] == ["https://cdn/1.mp4", "https://cdn/2.mp4"] and e_[0]["filename"] == "Trip 01")
t_, e_ = E.expand_collection("https://balbums.st/someuser", E.Config())
check("profile page: videos from each album", len(e_) == 2 and e_[0]["url"].startswith("https://cdn/"))

# --- queue behaviour -----------------------------------------------------------------------
cfg = E.Config(); cfg.download_dir = os.path.join(_tmp, "dl"); os.makedirs(cfg.download_dir, exist_ok=True)
m = E.Manager(cfg); m.save = lambda: None
started = []
m._start = lambda it: (started.append(it), setattr(it, "running", True))
a = m.add("https://example.com/v/1"); b = m.add("https://www.example.com/v/1/")
check("duplicate link is not added twice", a is b and len(m.items) == 1)
for i in range(2, 9): m.add(f"https://example.com/v/{i}")
m.tick(); check("per-site limit (3 at once)", len(started) == 1, len(started))   # gap rule: one start per 1.2 s
time.sleep(1.3); m.tick(); time.sleep(1.3); m.tick(); time.sleep(1.3); m.tick()
check("never more than 3 running per site", sum(1 for i in m.items if i.running) == 3)
m.items[0].running = False; m.items[0].status = "Completed"
m._host_last.clear(); m.tick(); check("a freed slot is reused", sum(1 for i in m.items if i.running) == 3)
# retry classification
check("429 is transient", E._transient("HTTP Error 429: Too Many Requests"))
check("timeout is transient", E._transient("The read operation timed out"))
check("unsupported is not transient", not E._transient("ERROR: Unsupported URL: https://x"))
check("private is not transient", not E._transient("Video is private. 503"))
# failed download gets a second try
m2 = E.Manager(cfg); m2.save = lambda: None
it = m2.add("https://example.org/v/9")
def boom(item, c): raise RuntimeError("HTTP Error 503: Service Unavailable")
E.run_download = boom
E._worker(it, cfg)
check("transient failure waits for a retry", it.status == "Retry later" and it.attempts == 1, it.status)
m2._start = lambda i: setattr(i, "status", "Downloading")
it.retry_at = time.time() - 1; m2.tick(); check("retry starts again", it.status == "Downloading", it.status)
it.attempts = 2; E._worker(it, cfg); check("gives up after two retries", it.status == "Failed")
# low disk space holds back downloads
m3 = E.Manager(cfg); m3.save = lambda: None; s3 = []; m3._start = lambda i: s3.append(i)
m3._free_checked = time.time(); m3._free_mb = 100
m3.add("https://example.net/v/1"); m3.tick()
check("low disk: nothing starts and a notice is shown", not s3 and "disk" in m3.notice.lower(), m3.notice)
# batch expansion: duplicates skipped, confirmation asked
m4 = E.Manager(cfg); m4.save = lambda: None; m4._start = lambda i: None
E.expand_collection = lambda url, c: ("Bob", [{"url": f"https://example.com/v/{i}", "referer": url, "filename": f"Bob {i}", "thumb": ""} for i in range(40)])
asked = []
m4.confirm = lambda g, n: (asked.append((g, n)), 10)[1]
m4.add("https://www.redgifs.com/users/bob"); time.sleep(.4); m4.tick()
check("big batch asks and keeps the chosen number", asked == [("Bob", 40)] and len(m4.items) == 10, (asked, len(m4.items)))
m4.add("https://www.redgifs.com/users/bob"); time.sleep(.4); m4.confirm = lambda g, n: n; m4.tick()
check("second paste skips what is already there", len(m4.items) == 40, len(m4.items))
m4.add("https://www.redgifs.com/users/bob"); time.sleep(.4); m4.tick()
check("pasting again adds nothing", len(m4.items) == 40 and "Nothing new" in m4.notice, m4.notice)

# --- best quality of a captured stream
_master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\n360p/i.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n1080p/i.m3u8\n"
_media = "#EXTM3U\n#EXTINF:6,\nseg0.ts\n"
_web = {"https://v.com/x/master.m3u8": _master, "https://v.com/x/480p/i.m3u8": _media, "https://v.com/x/720p/i.m3u8": _media}
E._quick_get = lambda u, r="": _web[u] if u in _web else (_ for _ in ()).throw(RuntimeError("404"))
check("quality: a master playlist is kept (audio stays)", E.upgrade_stream("https://v.com/x/master.m3u8") == "https://v.com/x/master.m3u8")
check("quality: low variant finds the master", E.upgrade_stream("https://v.com/x/480p/i.m3u8") == "https://v.com/x/master.m3u8")
del _web["https://v.com/x/master.m3u8"]
check("quality: low variant finds a higher one", E.upgrade_stream("https://v.com/x/480p/i.m3u8") == "https://v.com/x/720p/i.m3u8")
check("quality: cap respected", E.upgrade_stream("https://v.com/x/480p/i.m3u8", "", 480) == "https://v.com/x/480p/i.m3u8")
_sz = {"https://c.com/v_1080p.mp4": 9_000_000}
E._quick_get_head = lambda u, r="": _sz.get(u, 0)
check("quality: direct file finds the larger one", E.upgrade_stream("https://c.com/v_480p.mp4") == "https://c.com/v_1080p.mp4")
check("quality: nothing to find leaves it alone", E.upgrade_stream("https://c.com/abc.mp4") == "https://c.com/abc.mp4")

# --- Beeg: the highest resolution from the site's own list
_api = {"file": {"hls_resources": {"fl_cdn_240": "key=a/240", "fl_cdn_720": "key=a/720", "fl_cdn_1080": "key=a/1080", "fl_cdn_480": ""}}}
E._page_html = lambda url, referer="", headers=None, timeout=25: json.dumps(_api)
_bs = E.beeg_streams("https://beeg.com/-0123456789")
check("beeg: highest first", [h for h, _ in _bs] == [1080, 720, 240] and _bs[0][1] == "https://video.beeg.com/key=a/1080", _bs)
check("beeg: other site is not matched", E.beeg_streams("https://example.com/-123") == [])
_api = {"file": {}, "fc_facts": [{"id": 9, "hls_resources": {"fl_cdn_480": "x/480"}}, {"id": 3, "hls_resources": {"fl_cdn_720": "x/720"}}]}
check("beeg: falls back to the first fact", [h for h, _ in E.beeg_streams("https://beeg.com/video/55")] == [720])

# --- VR math -------------------------------------------------------------------------------
import vr
check("vr: name hint", vr.detect_mode("scene_180_3dh_LR.mp4", 3840, 1920) == "180sbs")
check("vr: normal video", vr.detect_mode("holiday.mp4", 1920, 1080) == "")
import numpy as np
sw, sh = 2000, 1000
C = np.zeros((sh, sw), np.uint32); C[:, :1000] = np.arange(1000, dtype=np.uint32)[None, :]; C[:, 1000:] = 0xFFFFFF
idx, bad = vr.build_map("180sbs", 320, 180, 0, 0, 90, sw, sh)
o = np.take(C.ravel(), idx).reshape(180, 320)
check("vr: centre of the left eye", abs(int(o[90, 160]) - 500) < 6)
check("vr: right eye never shown", not (o == 0xFFFFFF).any())
R = np.zeros((sh, sw), np.uint32); R[:, :] = np.arange(sh, dtype=np.uint32)[:, None]
idx, bad = vr.build_map("180sbs", 320, 180, 0, 30, 90, sw, sh)
check("vr: looking up shows the upper part", int(np.take(R.ravel(), idx).reshape(180, 320)[90, 160]) < 400)

# --- licensing -----------------------------------------------------------------------------
import licensing as L
check("licensing off while no key is set", L.enabled() is False or L.PUBLIC_KEY_HEX != "")
secret = os.urandom(32); pub = L.public_from_secret(secret).hex()
tok = L.make_key(secret)
check("license key verifies", L.parse_key(tok, pub) is not None)
check("tampered key is rejected", L.parse_key(tok[:-3] + ("AAA" if not tok.endswith("AAA") else "BBB"), pub) is None)

print(f"\n{len(FAILS)} failed" if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)
