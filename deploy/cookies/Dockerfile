# Keeps a logged-in YouTube session in a real Chromium profile on the server
# and exports its cookies for yt-dlp. See README "YouTube cookies".
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc novnc websockify python3 python3-websocket \
      fonts-dejavu fonts-noto-color-emoji ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*
COPY cookie_keeper.py entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
