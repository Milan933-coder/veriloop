"""Keyed reverse-image adapters (D-15): Cloud Vision WEB_DETECTION and Fact Check claims:imageSearch.

Both are complete and activate as soon as GOOGLE_VISION_API_KEY / GOOGLE_FACTCHECK_API_KEY appear in
`.env`; until then ToolClient answers `unconfigured` without a request. API keys travel only in the
request, never in the ToolClient key (so caches and cassettes are secret-free).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from veriloop.core.config import Settings
from veriloop.core.http import raise_for_status
from veriloop.core.tool_client import ToolCallResult, ToolClient

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
FACTCHECK_IMAGE_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:imageSearch"


async def vision_web_detection(
    tools: ToolClient, settings: Settings, image: bytes, sha256: str, max_results: int = 20
) -> ToolCallResult:
    async def fn() -> Any:
        body = {
            "requests": [
                {
                    "image": {"content": base64.b64encode(image).decode("ascii")},
                    "features": [{"type": "WEB_DETECTION", "maxResults": max_results}],
                }
            ]
        }
        resp = await tools.http.post(
            VISION_URL, params={"key": settings.google_vision_api_key.get_secret_value()}, json=body
        )
        raise_for_status(resp)
        return resp.json()

    return await tools.call("vision_web", "web_detection", {"sha256": sha256, "max": max_results}, fn)


@dataclass(frozen=True)
class VisionPage:
    url: str
    title: str | None
    match_type: str  # full | partial | page


def parse_vision(body: Any) -> tuple[list[VisionPage], list[str]]:
    """Pages with matching images (full > partial > page) and best-guess labels."""
    responses = (body or {}).get("responses") or [{}]
    web = (responses[0] or {}).get("webDetection") or {}
    pages: list[VisionPage] = []
    for page in web.get("pagesWithMatchingImages") or []:
        url = page.get("url")
        if not url:
            continue
        if page.get("fullMatchingImages"):
            kind = "full"
        elif page.get("partialMatchingImages"):
            kind = "partial"
        else:
            kind = "page"
        title = page.get("pageTitle")
        pages.append(VisionPage(str(url), _strip_tags(title) if title else None, kind))
    labels = [str(lbl.get("label")) for lbl in web.get("bestGuessLabels") or [] if lbl.get("label")]
    return pages, labels


def _strip_tags(text: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", text).strip()


async def factcheck_image_search(
    tools: ToolClient, settings: Settings, image_url: str, language: str | None = None
) -> ToolCallResult:
    """claims:imageSearch needs a publicly reachable image URL (not bytes)."""

    async def fn() -> Any:
        params = {"imageUri": image_url, "key": settings.google_factcheck_api_key.get_secret_value()}
        if language:
            params["languageCode"] = language
        resp = await tools.http.get(FACTCHECK_IMAGE_URL, params=params)
        raise_for_status(resp)
        return resp.json()

    return await tools.call("factcheck", "image_search", {"image_url": image_url, "lang": language}, fn)


@dataclass(frozen=True)
class ImageReview:
    url: str
    publisher: str
    title: str | None
    rating_text: str
    review_date: str | None
    claim_text: str | None
    claimant: str | None


def parse_image_search(body: Any) -> list[ImageReview]:
    out: list[ImageReview] = []
    for result in (body or {}).get("results") or []:
        claim = result.get("claim") or {}
        for review in claim.get("claimReview") or []:
            url = review.get("url")
            if not url:
                continue
            publisher = review.get("publisher") or {}
            out.append(
                ImageReview(
                    url=str(url),
                    publisher=str(publisher.get("name") or publisher.get("site") or ""),
                    title=review.get("title"),
                    rating_text=str(review.get("textualRating") or "unrated"),
                    review_date=review.get("reviewDate"),
                    claim_text=claim.get("text"),
                    claimant=claim.get("claimant"),
                )
            )
    return out
