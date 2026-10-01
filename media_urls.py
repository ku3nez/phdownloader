from urllib.parse import urlparse

PORNHUB_DOMAINS = ("pornhub.com", "pornhub.org")


def url_hostname(url: str | None) -> str:
    if not url:
        return ""
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except (TypeError, ValueError):
        return ""


def pornhub_domain(url: str | None) -> str | None:
    """Return the PornHub root domain for the URL, or None for other hosts."""
    hostname = url_hostname(url)
    for domain in PORNHUB_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return domain
    return None


def is_pornhub_url(url: str | None) -> bool:
    """Accept PornHub's supported primary domains and their subdomains only."""
    return pornhub_domain(url) is not None


YOUTUBE_DOMAINS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_youtube_url(url: str | None) -> bool:
    hostname = url_hostname(url)
    return any(hostname == domain or hostname.endswith("." + domain) for domain in YOUTUBE_DOMAINS)
