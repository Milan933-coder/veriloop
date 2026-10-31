"""Network fetches for ingestion, each routed through ToolClient (breaker, chaos, cache, cassettes).

Tool fns return raw JSON payloads (HTML text, oEmbed JSON, file metadata); parsing happens after the
call so live, cached and replayed results behave identically (D-12).
"""

from __future__ import annotations

import asyncio
import math
import shutil
from pathlib import Path
from typing import Any

import structlog

from veriloop.contracts import Platform, ToolError
from veriloop.core.errors import ToolAttemptError
from veriloop.core.http import raise_for_status
from veriloop.core.tool_client import ToolCallResult, ToolClient

from .pagemeta import oembed_request

log = structlog.get_logger(__name__)

# A browser-like UA (still naming us) gets the same og: tags a link preview sees; several news
# sites and platforms serve a bare bot UA an empty shell or a 403 (research.md).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 VeriLoop/0.1"
)
_MAX_HTML = 150_000  # meta tags live in <head>; keeps cassettes small
_MAX_IMAGE_BYTES = 15 * 2**20


async def expand_short_link(tools: ToolClient, url: str) -> ToolCallResult:
    """Follow redirects of a t.co/bit.ly-style link without downloading the target body."""

    async def fn() -> dict[str, Any]:
        resp = await tools.http.head(url, follow_redirects=True, headers={"User-Agent": BROWSER_UA})
        if resp.status_code in (403, 405, 501) or resp.status_code >= 500:
            async with tools.http.stream(
                "GET", url, follow_redirects=True, headers={"User-Agent": BROWSER_UA}
            ) as streamed:
                return {"final_url": str(streamed.url), "status": streamed.status_code}
        return {"final_url": str(resp.url), "status": resp.status_code}

    return await tools.call("page_fetch", "expand", {"url": url}, fn)


async def fetch_oembed(tools: ToolClient, platform: Platform, url: str) -> ToolCallResult | None:
    req = oembed_request(platform, url)
    if req is None:
        return None
    endpoint, params = req

    async def fn() -> Any:
        resp = raise_for_status(await tools.http.get(endpoint, params=params))
        return resp.json()

    return await tools.call("oembed", platform.value, {"endpoint": endpoint, **params}, fn)


async def fetch_page(tools: ToolClient, url: str) -> ToolCallResult:
    async def fn() -> dict[str, Any]:
        resp = await tools.http.get(
            url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8"}
        )
        raise_for_status(resp)
        ctype = resp.headers.get("content-type", "")
        html = resp.text[:_MAX_HTML] if ("html" in ctype or "xml" in ctype or not ctype) else ""
        return {"final_url": str(resp.url), "status": resp.status_code, "content_type": ctype, "html": html}

    return await tools.call("page_fetch", "page", {"url": url}, fn)


async def download_image(tools: ToolClient, url: str, dest: Path) -> ToolCallResult:
    """Stream an image to `dest` (capped size, image/* only). Payload = file metadata, not bytes."""

    async def fn() -> dict[str, Any]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        async with tools.http.stream("GET", url, headers={"User-Agent": BROWSER_UA}) as resp:
            raise_for_status(resp)
            ctype = resp.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                raise ToolAttemptError("parse_error", f"not an image (content-type {ctype or 'missing'})")
            chunks: list[bytes] = []
            size = 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > _MAX_IMAGE_BYTES:
                    raise ToolAttemptError("too_large", "image larger than 15 MB")
                chunks.append(chunk)
        await asyncio.to_thread(dest.write_bytes, b"".join(chunks))
        return {"path": str(dest), "content_type": ctype, "bytes": size, "final_url": str(resp.url)}

    return await tools.call("page_fetch", "image", {"url": url}, fn)


def _playwright_blocking(url: str, screenshot: Path, timeout_ms: int) -> dict[str, Any]:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    user_agent=BROWSER_UA, viewport={"width": 1280, "height": 1600}, locale="en-IN"
                )
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:  # give client-rendered captions a moment, but never block on network idle
                    page.wait_for_load_state("networkidle", timeout=min(5000, timeout_ms // 3))
                except PlaywrightError:
                    pass
                meta = page.evaluate(
                    """() => { const out = {};
                      for (const m of document.querySelectorAll('meta[property],meta[name]')) {
                        const k = m.getAttribute('property') || m.getAttribute('name');
                        if (k && !(k in out)) out[k] = m.getAttribute('content') || ''; }
                      return out; }"""
                )
                text = page.evaluate("() => (document.body && document.body.innerText) || ''")
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=False, type="jpeg", quality=85)
                return {
                    "final_url": page.url,
                    "status": response.status if response else None,
                    "title": page.title(),
                    "meta": {k: v for k, v in meta.items() if isinstance(v, str)},
                    "text": " ".join(str(text).split())[:4000],
                    "screenshot": str(screenshot),
                }
            finally:
                browser.close()
    except PlaywrightError as exc:
        msg = str(exc).split("\n")[0][:300]
        if "Timeout" in msg:
            raise ToolAttemptError("timeout", msg) from exc
        if "Executable doesn't exist" in msg:
            raise ToolAttemptError("unconfigured", "playwright chromium is not installed") from exc
        raise ToolAttemptError("http_error", msg) from exc


async def playwright_capture(tools: ToolClient, url: str, screenshot: Path, timeout_s: float = 25.0):
    """Headless Chromium render (E1 last resort): title, meta, visible text, viewport screenshot."""

    async def fn() -> dict[str, Any]:
        return await asyncio.to_thread(_playwright_blocking, url, screenshot, int(timeout_s * 1000))

    return await tools.call("playwright_fetch", "capture", {"url": url}, fn)


def localize_file(recorded_path: str | None, dest_dir: Path) -> Path | None:
    """Make a file named in a tool payload available inside this job's media dir.

    Live calls already wrote it there. Replayed/cached payloads name a file from the recording job:
    copy it when it still exists locally, otherwise report it missing (never pretend it exists).
    """
    if not recorded_path:
        return None
    src = Path(recorded_path)
    if not src.exists():
        return None
    try:
        if src.resolve().parent == dest_dir.resolve():
            return src
    except OSError:
        return None
    target = dest_dir / src.name
    if not target.exists():
        shutil.copy2(src, target)
    return target


def error_of(result: ToolCallResult | None) -> ToolError | None:
    return None if result is None or result.ok else result.error


def x_syndication_token(tweet_id: str) -> str:
    """The token the public embed widget sends: base36 of (id / 1e15 * pi) with zeros and '.' removed."""
    value = (int(tweet_id) / 1e15) * math.pi
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    whole, frac = int(value), value - int(value)
    head = ""
    while whole:
        head = digits[whole % 36] + head
        whole //= 36
    tail = ""
    for _ in range(12):
        frac *= 36
        tail += digits[int(frac)]
        frac -= int(frac)
    return f"{head or '0'}.{tail}".replace("0", "").replace(".", "")


async def fetch_x_syndication(tools: ToolClient, tweet_id: str) -> ToolCallResult:
    """Keyless tweet JSON (text, created_at, photos, video poster) used by X's own embed widget."""

    async def fn() -> Any:
        resp = await tools.http.get(
            "https://cdn.syndication.twimg.com/tweet-result",
            params={"id": tweet_id, "token": x_syndication_token(tweet_id), "lang": "en"},
        )
        raise_for_status(resp)
        return resp.json()

    return await tools.call("page_fetch", "x_syndication", {"id": tweet_id}, fn)
