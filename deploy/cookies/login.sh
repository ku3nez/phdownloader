#!/bin/bash
# One-time (or after Google signs the session out): open a browser on this
# server through noVNC and sign in to the YouTube account by hand.
#   deploy/cookies/login.sh start   -> prints the address and password
#   deploy/cookies/login.sh stop    -> closes the browser, then refreshes cookies
set -euo pipefail
PROFILE=/var/lib/phd-cookies/profile
case "${1:-start}" in
  start)
    mkdir -p "$PROFILE" && chmod 700 /var/lib/phd-cookies
    PASS=$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 8)
    docker rm -f phd-cookies-login >/dev/null 2>&1 || true
    docker run -d --name phd-cookies-login --shm-size=512m -p 6081:6081 -e VNC_PASSWORD="$PASS" \
      -v "$PROFILE":/profile phd-cookies login >/dev/null
    IP=$(curl -s -4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
    echo "Open http://$IP:6081/vnc.html  password: $PASS"
    ;;
  stop)
    docker rm -f phd-cookies-login >/dev/null 2>&1 || true
    systemctl start phdownloader-cookies.service
    cat /mnt/phdownloader/cookies/status.json; echo
    ;;
esac
