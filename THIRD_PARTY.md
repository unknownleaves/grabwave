# Third-party software bundled with Grabwave

| Component | Used for | License |
| --- | --- | --- |
| Qt / PySide6 (incl. Qt WebEngine / Chromium) | Window and built-in browser | LGPL-3.0 (Qt), BSD-style (Chromium) |
| yt-dlp | Reading and downloading videos | Unlicense |
| streamlink | Recording live streams | BSD-2-Clause |
| curl_cffi, requests, urllib3, certifi, websockets | Networking | MIT / Apache-2.0 / MPL-2.0 |
| FFmpeg | Merging, converting | LGPL-2.1+ / GPL-2+ depending on the build. Source and licence: https://ffmpeg.org/legal.html |

The ffmpeg build that ships in the installer is the "release essentials" build from https://www.gyan.dev/ffmpeg/builds/ (GPL v3).
