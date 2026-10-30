#!/bin/bash
# login   - Chromium on a virtual screen, reachable through noVNC on :6081, to
#           sign in to the Google account once by hand.
# refresh - headless Chromium with the same profile opens YouTube and writes
#           the current cookies to /out/youtube.txt.
set -euo pipefail
MODE="${1:-refresh}"
FLAGS=(--user-data-dir=/profile --password-store=basic --no-first-run --no-default-browser-check
       --disable-dev-shm-usage --no-sandbox --lang=ru-RU)
rm -f /profile/SingletonLock /profile/SingletonSocket /profile/SingletonCookie

if [ "$MODE" = login ]; then
  : "${VNC_PASSWORD:?VNC_PASSWORD is required}"
  Xvfb :1 -screen 0 1280x860x24 >/dev/null 2>&1 &
  export DISPLAY=:1
  sleep 1
  x11vnc -storepasswd "$VNC_PASSWORD" /tmp/vncpass >/dev/null
  x11vnc -display :1 -rfbauth /tmp/vncpass -forever -shared -rfbport 5900 -quiet >/dev/null 2>&1 &
  chromium "${FLAGS[@]}" --window-position=0,0 --window-size=1280,860 \
    "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fwww.youtube.com%2F" >/dev/null 2>&1 &
  echo "noVNC on :6081"
  exec websockify --web /usr/share/novnc 6081 localhost:5900
fi

chromium "${FLAGS[@]}" --headless=new --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
  about:blank >/tmp/chromium.log 2>&1 &
BROWSER=$!
status=0
python3 /app/cookie_keeper.py /out/youtube.txt || status=$?
kill "$BROWSER" 2>/dev/null || true
wait "$BROWSER" 2>/dev/null || true
exit $status
