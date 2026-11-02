"""Per-client limits against scripted enumeration of /start and /inspect.

Counters live in Redis, so all API nodes share them. A client that keeps
submitting links the server refuses (internal addresses and the like) is
banned for a day.
"""

import ipaddress
import os
import time

LIMITS = {
    # endpoint: (requests, window seconds)
    "start": (int(os.getenv("RATE_LIMIT_START", 30)), 600),
    "inspect": (int(os.getenv("RATE_LIMIT_INSPECT", 120)), 600),
}
REJECTED_BEFORE_BAN = int(os.getenv("BAN_AFTER_REJECTED_LINKS", 3))
BAN_SECONDS = int(os.getenv("BAN_SECONDS", 24 * 3600))
# Requests come either straight to the API or through the reverse proxy for
# the domain; only a private-network peer may say who the real client is.
TRUSTED_PROXY_NETS = [ipaddress.ip_network(n) for n in os.getenv(
    "TRUSTED_PROXY_NETS", "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16").split(",") if n.strip()]


def client_ip(request):
    peer = request.remote_addr or ""
    try:
        trusted = any(ipaddress.ip_address(peer) in net for net in TRUSTED_PROXY_NETS)
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("X-Real-IP") or (request.headers.get("X-Forwarded-For") or "").split(",")[0]
        if forwarded.strip():
            return forwarded.strip()
    return peer


def is_banned(redis, ip):
    return bool(redis.exists(f"phd:ban:{ip}"))


def over_limit(redis, kind, ip):
    limit, window = LIMITS[kind]
    key = f"phd:rl:{kind}:{ip}:{int(time.time() // window)}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, window + 5)
    return count > limit


def note_rejected(redis, ip):
    """Count a refused link; True when the client is now banned."""
    key = f"phd:rejected:{ip}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, 3600)
    if count >= REJECTED_BEFORE_BAN:
        redis.set(f"phd:ban:{ip}", str(int(time.time())), ex=BAN_SECONDS)
        return True
    return False
