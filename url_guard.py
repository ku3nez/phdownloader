"""Keep user-supplied links away from the servers' own network.

Anyone who can open the site can make the workers fetch a URL. Without
checks that reached cloud metadata (169.254.169.254) and services listening
on localhost. Two layers:

* check_public_url() rejects links whose host is or resolves to a private,
  loopback, link-local or otherwise non-public address, before queueing and
  again right before downloading;
* install() makes every Python socket connection in the process refuse such
  addresses, so redirects and DNS tricks are caught as well. Only explicitly
  allowed local endpoints (the YouTube SOCKS tunnel) stay reachable.
"""

import ipaddress
import os
import socket
import urllib.parse


class BlockedURL(ValueError):
    pass


def _allowed_local():
    allowed = set()
    for item in os.getenv("SSRF_ALLOW_LOCAL", "127.0.0.1:1080").split(","):
        item = item.strip()
        if ":" in item:
            host, port = item.rsplit(":", 1)
            if port.isdigit():
                allowed.add((host, int(port)))
    for name in ("YT_DLP_YOUTUBE_PROXY", "YT_DLP_PROXY"):
        proxy = os.getenv(name)
        if proxy:
            parsed = urllib.parse.urlparse(proxy)
            if parsed.hostname and parsed.port:
                allowed.add((parsed.hostname, parsed.port))
    return allowed


ALLOWED_LOCAL = _allowed_local()


def _public(address):
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_public_url(url):
    """Raise BlockedURL unless url is http(s) to a public host."""
    parsed = urllib.parse.urlparse(str(url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BlockedURL("Only http(s) links are supported")
    host = parsed.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise BlockedURL("This address is not allowed")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise BlockedURL("Invalid port")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise BlockedURL("The site's address could not be found")
    if not infos or not all(_public(info[4][0]) for info in infos):
        raise BlockedURL("This address is not allowed")
    return url


_original_getaddrinfo = socket.getaddrinfo


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    infos = _original_getaddrinfo(host, port, *args, **kwargs)
    try:
        port_number = int(port) if port is not None else 0
    except (TypeError, ValueError):
        port_number = 0
    if (str(host), port_number) in ALLOWED_LOCAL:
        return infos
    safe = [info for info in infos if _public(info[4][0]) or (info[4][0], port_number) in ALLOWED_LOCAL]
    if not safe:
        raise socket.gaierror(socket.EAI_NONAME, f"Blocked non-public address for {host}")
    return safe


def install(extra_allowed=()):
    """Refuse connections to non-public addresses from this process.

    extra_allowed: (host, port) pairs the process itself needs, e.g. Redis.
    """
    for host, port in extra_allowed:
        try:
            for info in _original_getaddrinfo(host, port):
                ALLOWED_LOCAL.add((info[4][0], int(port)))
        except Exception:
            pass
        ALLOWED_LOCAL.add((host, int(port)))
    socket.getaddrinfo = _guarded_getaddrinfo
