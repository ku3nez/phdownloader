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
# Requests reach the API straight or through a chain of reverse proxies
# (front -> relay -> local nginx), each appending to X-Forwarded-For. The
# client is the right-most address that is not one of our proxies; anything
# left of it may have been written by the client itself.
TRUSTED_PROXY_NETS = [ipaddress.ip_network(n.strip()) for n in os.getenv(
    "TRUSTED_PROXY_NETS",
    "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "138.124.254.81/32,77.238.249.189/32,194.246.82.114/32,91.84.110.55/32,81.88.23.239/32,"
    "57.131.195.41/32,85.190.126.94/32").split(",") if n.strip()]


def _trusted(address):
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    return any(ip in net for net in TRUSTED_PROXY_NETS)


def client_ip(request):
    peer = request.remote_addr or ""
    if not _trusted(peer):
        return peer
    chain = [part.strip() for part in (request.headers.get("X-Forwarded-For") or "").split(",") if part.strip()]
    for address in reversed(chain):
        if not _trusted(address):
            return address
    return chain[0] if chain else peer


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
