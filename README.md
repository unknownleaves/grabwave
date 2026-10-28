# Grabwave

Download videos and record live streams (up to 20 at a time) in a MeGet-style window with a built-in browser.

## For users
| System | File | How to install |
| --- | --- | --- |
| Windows | `Grabwave-Setup-x.y.exe` | Double-click, Next, Finish. Start menu + desktop shortcut. Uninstall from Settings > Apps. |
| macOS (Apple Silicon) | `Grabwave-x.y.dmg` | Open it, drag Grabwave to Applications. First launch: right-click the app > Open. |
| Linux | `Grabwave-x.y-x86_64.AppImage` | Right-click > Properties > allow executing, then double-click. |

Everything needed (including ffmpeg) is inside. Apps without a paid code-signing certificate trigger a one-time
warning: Windows "Run anyway" (More info), macOS right-click > Open.

**Sites stop working?** Settings > *Video engine* > **Update now**, then restart. This refreshes yt-dlp without reinstalling.

## Getting the installers built (maintainer)
The three installers must be built on their own systems; GitHub does that for free:
1. Put this folder in a GitHub repository.
2. Actions tab > *build installers* > **Run workflow** (or push a tag such as `v1.6` to publish a Release).
3. When it finishes, download the `.exe`, `.dmg` and `.AppImage` from the run's Artifacts (or from the Release).

Local builds: Windows `build_installer.bat`, macOS `build_mac.sh` (on a Mac), Linux `build_linux.sh`.

## Running from source (development)
```
py -3 -m venv .venv && .venv\Scripts\activate && pip install -r requirements.txt && python app.py
```
Put ffmpeg in an `ffmpeg` folder next to `app.py`.

## Sites whose yt-dlp extractor is broken (e.g. Beeg)
Open the video in the built-in browser, let it start loading, then click the Download button on the page (or the
address-bar download button). Grabwave remembers the stream address the page requested and uses it when downloading
from the page link fails.

## Files
`app.py` window - `engine.py` queue, downloads, recording, updates - `camsites.py` live-room lookups -
`Grabwave.spec` PyInstaller recipe - `installer/Grabwave.iss` Windows installer, `packaging/` macOS and Linux packaging - `assets/` icon (`make_icon.py` redraws it).
