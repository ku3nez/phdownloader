"""Open YouTube in the running headless Chromium and export its cookies.

Writes a Netscape cookies.txt (what yt-dlp reads) atomically and a small
status JSON next to it. Exit code 2 means the profile is not signed in.
"""
import json
import os
import sys
import time
import urllib.request

import websocket

DEBUG = "http://127.0.0.1:9222"
DOMAINS = ("youtube.com", "google.com")


def wait_for_browser(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return json.load(urllib.request.urlopen(f"{DEBUG}/json/version", timeout=3))
        except Exception:
            time.sleep(1)
    raise RuntimeError("Chromium did not start")


class Tab:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.counter = 0

    def call(self, method, **params):
        self.counter += 1
        self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})


def netscape(cookies):
    lines = ["# Netscape HTTP Cookie File", "# Exported by phdownloader cookie keeper", ""]
    for c in cookies:
        domain = c["domain"]
        expires = int(c.get("expires") or 0)
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path") or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(max(expires, 0)),
            c["name"],
            c["value"],
        ]))
    return "\n".join(lines) + "\n"


def load_netscape(path):
    """Netscape cookies.txt -> DevTools CookieParam list."""
    cookies = []
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path_, secure, expires, name, value = parts[:7]
        if not domain.lstrip(".").endswith(DOMAINS):
            continue
        cookie = {"name": name, "value": value, "domain": domain, "path": path_ or "/",
                  "secure": secure.upper() == "TRUE", "httpOnly": http_only}
        if expires.isdigit() and int(expires) > 0:
            cookie["expires"] = int(expires)
        if name.startswith("__Secure-") or name.startswith("__Host-"):
            cookie["secure"] = True
        cookies.append(cookie)
    return cookies


def main(out_path, import_path=None):
    wait_for_browser()
    if import_path:
        # Put cookies from a browser on another computer into this profile;
        # from then on the profile keeps the session going by itself.
        request = urllib.request.Request(f"{DEBUG}/json/new?about:blank", method="PUT")
        blank = Tab(json.load(urllib.request.urlopen(request, timeout=30))["webSocketDebuggerUrl"])
        cookies = load_netscape(import_path)
        blank.call("Network.enable")
        blank.call("Network.setCookies", cookies=cookies)
        print(f"imported {len(cookies)} cookies")
    request = urllib.request.Request(f"{DEBUG}/json/new?https://www.youtube.com/", method="PUT")
    target = json.load(urllib.request.urlopen(request, timeout=30))
    tab = Tab(target["webSocketDebuggerUrl"])
    tab.call("Page.enable")
    time.sleep(12)  # let YouTube load and refresh its session cookies
    try:
        cookies = tab.call("Network.getAllCookies").get("cookies", [])
    except RuntimeError:
        cookies = tab.call("Storage.getCookies").get("cookies", [])
    cookies = [c for c in cookies if c["domain"].lstrip(".").endswith(DOMAINS)]
    names = {c["name"] for c in cookies}
    signed_in = "LOGIN_INFO" in names and ("SAPISID" in names or "__Secure-3PAPISID" in names)
    status = {"updated_at": int(time.time()), "cookies": len(cookies), "signed_in": signed_in}
    status_path = os.path.join(os.path.dirname(out_path), "status.json")
    if not signed_in:
        status["error"] = "The browser profile is not signed in to YouTube"
        with open(status_path, "w") as handle:
            json.dump(status, handle)
        print(status["error"], file=sys.stderr)
        return 2
    temp = out_path + ".tmp"
    with open(temp, "w") as handle:
        handle.write(netscape(cookies))
    os.chmod(temp, 0o600)
    os.replace(temp, out_path)
    with open(status_path, "w") as handle:
        json.dump(status, handle)
    print(f"exported {len(cookies)} cookies")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
