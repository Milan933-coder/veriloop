"""URL normalisation and platform detection (pure functions, no I/O).

Normalising matters for three reasons: cache/cassette keys (the same post shared with different
tracking params must hit the same entry), oEmbed endpoints (which reject share-sheet variants), and
prior-run matching in provenance.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from veriloop.contracts import Platform

# Query parameters that only identify the sharer / campaign, never the content (any site).
_TRACKING_GLOBAL = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "igshid",
    "igsh",
    "mibextid",
    "__tn__",
    "__cft__",
    "rdid",
    "share_url",
    "_rdr",
    "wt_mc",
    "cmpid",
    "ito",
    "spm",
}
# Share-sheet params that are only safe to drop on the big social platforms: on arbitrary sites
# `s`, `t`, `ref` or `src` can be real content parameters (search terms, article ids).
_TRACKING_SOCIAL = {
    "si",
    "feature",
    "ref",
    "ref_src",
    "ref_url",
    "src",
    "s",
    "t",
    "app",
    "is_from_webapp",
    "sender_device",
    "web_id",
    "utm_source",
}
_TRACKING_PREFIXES = ("utm_", "__cft__", "__tn__", "mc_", "pk_", "hsa_")

# Hosts that only redirect to the real URL (expanded with a HEAD/GET through ToolClient page_fetch).
SHORT_LINK_HOSTS = frozenset(
    {
        "t.co",
        "bit.ly",
        "bitly.com",
        "tinyurl.com",
        "goo.gl",
        "ow.ly",
        "buff.ly",
        "fb.me",
        "fb.watch",
        "lnkd.in",
        "shorturl.at",
        "rb.gy",
        "is.gd",
        "cutt.ly",
        "t.ly",
        "tiny.cc",
        "rebrand.ly",
        "vm.tiktok.com",
        "vt.tiktok.com",
        "dlvr.it",
        "trib.al",
        "amzn.to",
        "shorturl.me",
    }
)

_PLATFORM_HOSTS: tuple[tuple[Platform, tuple[str, ...]], ...] = (
    (Platform.X, ("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "t.co")),
    (Platform.INSTAGRAM, ("instagram.com", "instagr.am")),
    (Platform.FACEBOOK, ("facebook.com", "fb.com", "fb.watch", "fb.me")),
    (Platform.YOUTUBE, ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    (Platform.TIKTOK, ("tiktok.com",)),
    (Platform.TELEGRAM, ("t.me", "telegram.me", "telegram.dog")),
    (Platform.WHATSAPP, ("wa.me", "whatsapp.com")),
)

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'\]\)]+", re.IGNORECASE)


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def detect_platform(url: str) -> Platform:
    host = _host(url)
    for platform, domains in _PLATFORM_HOSTS:
        if any(_host_matches(host, d) for d in domains):
            return platform
    return Platform.WEB


def is_short_link(url: str) -> bool:
    host = _host(url)
    return any(_host_matches(host, d) for d in SHORT_LINK_HOSTS)


def youtube_video_id(url: str) -> str | None:
    """Video id from watch / shorts / youtu.be / embed / live URLs, else None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.split("/") if p]
    candidate: str | None = None
    if _host_matches(host, "youtu.be") and path:
        candidate = path[0]
    elif _host_matches(host, "youtube.com") or _host_matches(host, "youtube-nocookie.com"):
        if path and path[0] in {"shorts", "embed", "live", "v", "e"} and len(path) > 1:
            candidate = path[1]
        else:
            candidate = dict(parse_qsl(parts.query)).get("v")
    return candidate if candidate and _YT_ID.fullmatch(candidate) else None


def _clean_query(query: str, *, social: bool) -> str:
    drop = _TRACKING_GLOBAL | _TRACKING_SOCIAL if social else _TRACKING_GLOBAL
    kept = [
        (k, v)
        for k, v in parse_qsl(query, keep_blank_values=False)
        if k.lower() not in drop and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    return urlencode(kept, doseq=True)


def normalize_url(url: str) -> str:
    """Canonical form: https, lower-case host, no tracking params/fragment, platform canonical hosts.

    X/Twitter mirrors become x.com; every YouTube video form becomes watch?v=<id> (keeping `t`);
    mobile subdomains are dropped for the big platforms so oEmbed and caching see one URL per post.
    """
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    scheme = "https" if parts.scheme.lower() in {"http", "https"} else parts.scheme.lower()
    host = (parts.hostname or "").lower().rstrip(".")
    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    path = parts.path or "/"

    video_id = youtube_video_id(url)
    if video_id:
        start = dict(parse_qsl(parts.query)).get("t")
        query = urlencode({"v": video_id, **({"t": start} if start else {})})
        return urlunsplit(("https", "www.youtube.com", "/watch", query, ""))

    platform = detect_platform(url)
    if platform is Platform.X and host != "t.co":
        host = "x.com"
        path = re.sub(r"/photo/\d+$|/video/\d+$", "", path)
    elif platform is Platform.INSTAGRAM:
        host = "www.instagram.com"
    elif platform is Platform.FACEBOOK and host.startswith(("m.", "mobile.", "mbasic.", "web.")):
        host = "www.facebook.com"
    elif platform is Platform.TIKTOK and host.startswith("m."):
        host = "www.tiktok.com"

    if len(path) > 1 and path.endswith("/") and platform in (Platform.X, Platform.YOUTUBE):
        path = path.rstrip("/")
    social = platform is not Platform.WEB
    return urlunsplit((scheme, host + port, path, _clean_query(parts.query, social=social), ""))


def oembed_lookup_url(url: str) -> str:
    """The URL form oEmbed providers accept (YouTube oEmbed rejects /shorts/ ids in some regions)."""
    video_id = youtube_video_id(url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def extract_urls(text: str) -> list[str]:
    """http(s) URLs found in free text, trailing punctuation trimmed, order preserved, deduped."""
    seen: list[str] = []
    for match in _URL_IN_TEXT.findall(text or ""):
        cleaned = match.rstrip(".,;:!?…")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def text_without_urls(text: str) -> str:
    return re.sub(r"\s+", " ", _URL_IN_TEXT.sub(" ", text or "")).strip()
