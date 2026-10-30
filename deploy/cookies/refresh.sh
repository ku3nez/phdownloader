#!/bin/bash
# Run by phdownloader-cookies.timer: export fresh YouTube cookies from the
# signed-in server-side browser profile, then check that yt-dlp accepts them.
set -uo pipefail
PROFILE=/var/lib/phd-cookies/profile
OUT=/mnt/phdownloader/cookies
mkdir -p "$OUT"
if docker ps --format '{{.Names}}' | grep -qx phd-cookies-login; then
  echo "login browser is open; skipping refresh"; exit 0
fi
docker run --rm --name phd-cookies-refresh --shm-size=512m -v "$PROFILE":/profile -v "$OUT":/out phd-cookies refresh
status=$?
cd /opt/phdownloader && venv/bin/python cookie_check.py "$OUT/youtube.txt" "$OUT/status.json"
exit $status
