"""Check that the exported YouTube cookies work; record the result.

Usage: cookie_check.py <cookies.txt> <status.json>
"""
import json
import os
import shutil
import sys
import tempfile
import time

import yt_dlp

TEST_URL = os.getenv("YOUTUBE_COOKIE_TEST_URL", "https://www.youtube.com/watch?v=jNQXAC9IVRw")


def main(cookie_path, status_path):
    try:
        with open(status_path) as handle:
            status = json.load(handle)
    except Exception:
        status = {}
    status["checked_at"] = int(time.time())
    if not os.path.exists(cookie_path):
        status.update(check_ok=False, check_error="no cookie file")
    else:
        # yt-dlp writes cookies back; test on a copy.
        handle, temp = tempfile.mkstemp(suffix=".txt")
        os.close(handle)
        shutil.copyfile(cookie_path, temp)
        options = {
            "quiet": True, "no_warnings": True, "skip_download": True, "cookiefile": temp,
            "js_runtimes": {"deno": {}, "node": {}}, "remote_components": ["ejs:github"],
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(TEST_URL, download=False)
            status.update(check_ok=bool(info.get("formats")), check_error=None)
        except Exception as exc:
            status.update(check_ok=False, check_error=str(exc)[:300])
        finally:
            os.remove(temp)
    with open(status_path + ".tmp", "w") as handle:
        json.dump(status, handle)
    os.replace(status_path + ".tmp", status_path)
    print(json.dumps(status))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
