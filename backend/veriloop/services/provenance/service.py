"""run_provenance: own pHash/CLIP index first, then keyed reverse-image tools, then a first-seen estimate.

1. sha256 / pHash (Hamming <= 10) / CLIP (cosine >= 0.93) against known fakes + every prior run's media
   (cheap, local, always first; SPEC §1). This job's CLIP vectors are stored for future runs.
2. Cloud Vision web detection (vision_web) and Fact Check imageSearch (factcheck) when keyed.
3. Appearances with timestamps -> earliest one -> out_of_context score vs the claimed event date.
4. Context flags when a matched page's domain is on the satire / state-affiliated / known-disinfo lists.
Matched fact-check URLs become citable items P1.. (URLs come from the seed manifest or tool payloads,
never synthesized).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
import structlog
from rapidfuzz import fuzz

from veriloop.contracts import (
    Appearance,
    BranchStatus,
    ClaimExtraction,
    ClaimReviewInfo,
    EvidenceItem,
    MediaAsset,
    ProvenanceResult,
    ResolvedInput,
    RetrievalMethod,
    Stance,
    ToolError,
)
from veriloop.core.errors import MLUnavailable
from veriloop.core.executor import run_cpu
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.ingestion.fetchers import fetch_page
from veriloop.services.ingestion.imaging import hamming, is_informative, load_rgb, to_jpeg_bytes
from veriloop.services.ingestion.pagemeta import parse_date, parse_page_meta

from . import clip, index, sources, timeline, web

log = structlog.get_logger(__name__)

INDEX_TOOL = "veriloop_media_index"
INDEX_VERSION = "phash64+clip-vit-b32 (seed manifest 2026-10-31)"
MAX_ASSETS = 16
MAX_REMOTE_IMAGES = 2
MAX_PAGE_DATES = 5
STANCE_MIN_SIMILARITY = 0.6


# =============================================================================================
async def run_provenance(
    job_id: str, resolved: ResolvedInput, extraction: ClaimExtraction, deps: Deps
) -> ProvenanceResult:
    started = time.perf_counter()
    try:
        result = await _run(job_id, resolved, extraction, deps)
    except Exception as exc:  # never raise to the orchestrator
        log.exception("run_provenance_failed", job_id=job_id, error=f"{type(exc).__name__}: {exc}")
        result = ProvenanceResult(
            branch_status=BranchStatus.FAILED,
            errors=[
                ToolError(
                    tool=INDEX_TOOL,
                    op="run",
                    error_type="exception",
                    message=f"{type(exc).__name__}: {exc}"[:500],
                )
            ],
        )
    result.latency_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "run_provenance_done",
        job_id=job_id,
        branch_status=result.branch_status.value,
        known_fakes=[(m.known_fake_id, m.hamming, m.clip_similarity) for m in result.known_fake_matches],
        prior_runs=result.prior_run_matches,
        out_of_context=result.out_of_context_score,
        items=len(result.items),
        latency_ms=result.latency_ms,
    )
    return result


class _Laps:
    """Per-step wall time (ms) for the run log: shows whether CLIP, the index or a keyed tool dominates."""

    def __init__(self) -> None:
        self.laps: dict[str, int] = {}
        self._t = time.perf_counter()

    def lap(self, name: str) -> None:
        now = time.perf_counter()
        self.laps[name] = int((now - self._t) * 1000)
        self._t = now


def _prepare_blocking(assets: list[MediaAsset]) -> list[tuple[MediaAsset, Any]]:
    """Decode and keep only informative images (flat/black frames would match everything)."""
    out: list[tuple[MediaAsset, Any]] = []
    for asset in assets:
        try:
            img = load_rgb(asset.path)
        except ValueError:
            continue
        if is_informative(img):
            out.append((asset, img))
    return out


def _distinct(assets: list[MediaAsset]) -> list[MediaAsset]:
    chosen: list[MediaAsset] = []
    for asset in assets:
        if asset.phash and any(c.phash and hamming(asset.phash, c.phash) <= 2 for c in chosen):
            continue
        chosen.append(asset)
    return chosen[:MAX_ASSETS]


async def _run(
    job_id: str, resolved: ResolvedInput, extraction: ClaimExtraction, deps: Deps
) -> ProvenanceResult:
    notes: list[str] = []
    errors: list[ToolError] = []
    tools_used: dict[str, str] = {}
    clock = _Laps()
    assets = _distinct([a for a in resolved.media if a.source != "page_capture"])
    if not assets:
        return ProvenanceResult(
            branch_status=BranchStatus.SKIPPED, notes=["No image or video to trace (text-only input)"]
        )

    prepared = await run_cpu(_prepare_blocking, assets)
    clock.lap("decode")
    if len(prepared) < len(assets):
        notes.append(f"{len(assets) - len(prepared)} near-blank frame(s) skipped for matching")
    usable = [a for a, _ in prepared]

    vectors: dict[str, np.ndarray] = {}
    if deps.settings.ml_enable and prepared:
        try:
            mat = await run_cpu(clip.embed_images_blocking, [img for _, img in prepared], deps.settings)
            vectors = {a.id: mat[i] for i, (a, _) in enumerate(prepared)}
            tools_used[clip.MODEL_ID] = "laion2b_s34b_b79k"
        except Exception as exc:
            errors.append(
                ToolError(tool="clip_index", op="embed", error_type="exception", message=str(exc)[:300])
            )
            notes.append("CLIP unavailable: pHash-only matching")
    elif not deps.settings.ml_enable:
        notes.append("CLIP skipped (ML_ENABLE=false): pHash-only matching")
    clock.lap("clip_embed")

    branch = BranchStatus.OK
    try:
        found = await index.search(deps.db, job_id, usable, vectors)
        tools_used[INDEX_TOOL] = INDEX_VERSION
    except Exception as exc:
        errors.append(ToolError(tool=INDEX_TOOL, op="search", error_type="exception", message=str(exc)[:300]))
        found = index.IndexResult()
        branch = BranchStatus.INSUFFICIENT
    if vectors:
        await _store_vectors(deps, job_id, usable, vectors)
    clock.lap("index_search")

    matches = sorted(
        found.fakes.values(), key=lambda h: (h.hamming if h.hamming is not None else 99, -(h.clip or 0))
    )
    appearances: list[Appearance] = []
    items: list[EvidenceItem] = []
    similarities = await _claim_similarities(
        extraction, [m.primary.claim or m.primary.title or "" for m in matches], deps
    )
    for hit, sim in zip(matches, similarities, strict=True):
        appearances.extend(_fake_appearances(hit))
        items.extend(_fake_items(hit, sim, start=len(items) + 1))
        notes.append(_match_note(hit))

    clock.lap("claim_similarity")
    prior_ids = sorted(found.prior, key=lambda j: min(found.prior[j].hamming or 99, 99))
    appearances.extend(await _prior_appearances(deps, prior_ids[:5]))

    remote_items, remote_apps = await _keyed_sources(
        deps, resolved, extraction, prepared, notes, errors, tools_used, len(items)
    )
    items.extend(remote_items)
    appearances.extend(remote_apps)
    clock.lap("keyed_tools")

    flags: list[str] = []
    for app in appearances:
        for hit in sources.context_for(app.url):
            if hit.flag not in app.context_flags:
                app.context_flags.append(hit.flag)
            if hit.flag not in flags:
                flags.append(hit.flag)
            notes.append(
                f"Context: {app.domain or hit.domain} is listed as {hit.flag} ({hit.name}; {hit.source})"
            )

    now = datetime.now(UTC)
    first = timeline.earliest(appearances)
    event, explicit = timeline.claimed_event_date(extraction, now)
    ooc = timeline.out_of_context_score(first.first_seen_at if first else None, event, explicit=explicit)
    if ooc > 0 and first is not None and event is not None:
        basis = (
            "the date stated in the claim"
            if explicit
            else "the submission date (claim states no date; discounted)"
        )
        notes.append(
            f"Earliest appearance {first.first_seen_at:%Y-%m-%d} ({first.timestamp_source}) predates "
            f"{basis} {event:%Y-%m-%d}: out-of-context score {ooc:.2f}"
        )
    if resolved.published_at and resolved.normalized_url:
        notes.append(f"This post itself was published {resolved.published_at:%Y-%m-%d} (platform metadata)")
    if not matches and not found.prior:
        notes.append("No match in the known-fake index or earlier VeriLoop runs")

    if errors and branch is BranchStatus.OK:
        branch = BranchStatus.DEGRADED
    log.info("provenance_timings", job_id=job_id, assets=len(usable), timings_ms=clock.laps)
    return ProvenanceResult(
        branch_status=branch,
        first_seen_estimate=first,
        appearances=appearances,
        known_fake_matches=[m.to_match() for m in matches],
        prior_run_matches=prior_ids,
        out_of_context_score=ooc,
        context_flags=flags,
        items=items,
        notes=list(dict.fromkeys(notes)),
        errors=errors,
        tools_used=tools_used,
    )


async def _store_vectors(
    deps: Deps, job_id: str, assets: list[MediaAsset], vectors: dict[str, np.ndarray]
) -> None:
    try:
        await repo.insert_media_assets(deps.db, job_id, [a for a in assets if a.id in vectors], clip=vectors)
    except Exception as exc:  # no jobs row (scripts) -> nothing to attach the vectors to
        log.info("clip_vectors_not_stored", job_id=job_id, error=f"{type(exc).__name__}: {exc}"[:200])


# =============================================================================================
# known-fake matches -> appearances + citable items
# =============================================================================================
def _match_note(hit: index.FakeHit) -> str:
    parts = []
    if "sha256" in hit.via:
        parts.append("identical file (sha256)")
    if hit.hamming is not None:
        parts.append(f"pHash Hamming {hit.hamming}")
    if hit.clip is not None:
        parts.append(f"CLIP {hit.clip:.3f}")
    return (
        f"Known-fake index match {hit.base} ({hit.primary.label}): {', '.join(parts)}; "
        f"{len(hit.assets)} of this input's image(s)/frame(s) matched"
    )


def _fake_appearances(hit: index.FakeHit) -> list[Appearance]:
    match_type = "phash" if hit.hamming is not None else "clip"
    out: list[Appearance] = []
    for url, when in hit.origins():
        out.append(
            Appearance(
                url=url,
                first_seen_at=when,
                timestamp_source="known_fake_manifest",
                match_type=match_type,
                domain=sources.registrable_domain(url),
                page_title=hit.primary.title,
            )
        )
    if not out and hit.primary.source_urls:
        url = hit.primary.source_urls[0]
        out.append(
            Appearance(
                url=url,
                first_seen_at=hit.primary.first_seen_at,
                timestamp_source="known_fake_manifest",
                match_type=match_type,
                domain=sources.registrable_domain(url),
                page_title=hit.primary.title,
            )
        )
    return out


def _fake_items(hit: index.FakeHit, similarity: float, *, start: int) -> list[EvidenceItem]:
    fake = hit.primary
    method = RetrievalMethod.PHASH_INDEX if hit.hamming is not None else RetrievalMethod.CLIP_INDEX
    strength = []
    if hit.hamming is not None:
        strength.append(f"pHash Hamming {hit.hamming}")
    if hit.clip is not None:
        strength.append(f"CLIP similarity {hit.clip:.2f}")
    label_text = f"Index label: {fake.label}"
    refutes = similarity >= STANCE_MIN_SIMILARITY and fake.label in ("manipulated", "out_of_context")
    items: list[EvidenceItem] = []
    for url in fake.source_urls:
        pub = sources.publisher_for(url)
        snippet = (
            f"VeriLoop known-fake index entry {hit.base} matches this input's media ({'; '.join(strength)}). "
            f'Indexed claim: "{fake.claim or fake.title or ""}". {label_text}. '
            f"Fact-check listed for this entry in the seed manifest: {url}"
        )
        items.append(
            EvidenceItem(
                id=f"P{start + len(items)}",
                source_name=pub.name,
                url=url,
                domain=sources.registrable_domain(url),
                publisher_group=sources.publisher_group(url),
                title=fake.title,
                snippet=snippet,
                published_at=None,
                confidence_relevance=round(similarity, 3),
                stance=Stance.REFUTE if refutes else Stance.NEUTRAL,
                stance_confidence=round(min(0.95, similarity), 3) if refutes else 0.0,
                stance_quote=label_text if refutes else None,
                retrieval_method=method,
                tool=INDEX_TOOL,
                tool_version=INDEX_VERSION,
                branch="provenance",
                is_fact_check=pub.is_fact_check,
                is_government_source=pub.is_government,
                claimreview=ClaimReviewInfo(
                    publisher=pub.name,
                    rating_text=f"{fake.label} (VeriLoop known-fake index, seed manifest)",
                    normalized_rating=-1.0 if fake.label in ("manipulated", "out_of_context") else 0.0,
                    manipulation_rating=fake.label == "manipulated",
                    claim_text=fake.claim,
                    similarity=round(similarity, 4),
                ),
            )
        )
    return items


async def _claim_similarities(extraction: ClaimExtraction, texts: list[str], deps: Deps) -> list[float]:
    """0..1 similarity of this job's primary claim to each indexed claim (e5 cosine, rescaled)."""
    if not texts:
        return []
    claim = extraction.primary_claim()
    query = (claim.text_en or claim.text) if claim else (extraction.caption_text or "")
    if not query.strip():
        return [0.0] * len(texts)
    if deps.embedder.available:
        try:
            q = await deps.embedder.embed([query], kind="query")
            p = await deps.embedder.embed([t or " " for t in texts], kind="passage")
            return [_rescale(float(np.dot(q[0], row))) for row in p]
        except MLUnavailable:
            pass
    # Lexical fallback is conservative (x0.8): paraphrases score lower than with embeddings.
    return [round(0.8 * fuzz.token_set_ratio(query.lower(), (t or "").lower()) / 100.0, 4) for t in texts]


def _rescale(cosine: float) -> float:
    try:
        from veriloop.services.evidence.text import claim_similarity

        return round(claim_similarity(cosine), 4)
    except Exception:  # same mapping as the evidence branch: e5 cosine 0.80 -> 0, 0.95 -> 1
        return round(max(0.0, min(1.0, (cosine - 0.80) / 0.15)), 4)


# =============================================================================================
# prior runs
# =============================================================================================
async def _prior_appearances(deps: Deps, job_ids: list[str]) -> list[Appearance]:
    out: list[Appearance] = []
    for prior_id in job_ids:
        try:
            row = await repo.get_job(deps.db, prior_id)
        except Exception:
            continue
        if row is None or row.redacted:
            continue
        url = (row.request or {}).get("url")
        if not url:
            continue
        out.append(
            Appearance(
                url=str(url),
                first_seen_at=row.created_at,
                timestamp_source="veriloop_prior_run",
                match_type="phash",
                domain=sources.registrable_domain(str(url)),
            )
        )
    return out


# =============================================================================================
# keyed tools (D-15): Cloud Vision web detection + Fact Check imageSearch
# =============================================================================================
async def _keyed_sources(
    deps: Deps,
    resolved: ResolvedInput,
    extraction: ClaimExtraction,
    prepared: list[tuple[MediaAsset, Any]],
    notes: list[str],
    errors: list[ToolError],
    tools_used: dict[str, str],
    item_offset: int,
) -> tuple[list[EvidenceItem], list[Appearance]]:
    items: list[EvidenceItem] = []
    apps: list[Appearance] = []
    tools = deps.tools
    if tools.is_configured("vision_web") and prepared:
        tools_used["vision_web"] = tools.tool_versions().get("vision_web", "")
        for asset, img in prepared[:MAX_REMOTE_IMAGES]:
            payload = await run_cpu(to_jpeg_bytes, img, quality=90)
            res = await web.vision_web_detection(tools, deps.settings, payload, asset.sha256)
            if not res.ok:
                if res.error:
                    errors.append(res.error)
                break
            pages, labels = web.parse_vision(res.value)
            if labels:
                notes.append("Cloud Vision best guess: " + ", ".join(labels[:3]))
            for page in pages[:10]:
                if any(a.url == page.url for a in apps):
                    continue
                apps.append(
                    Appearance(
                        url=page.url,
                        page_title=page.title,
                        match_type=page.match_type,
                        domain=sources.registrable_domain(page.url),
                    )
                )
        await _date_pages(deps, apps, errors)
        for app in apps:
            pub = sources.publisher_for(app.url)
            items.append(
                EvidenceItem(
                    id=f"P{item_offset + len(items) + 1}",
                    source_name=pub.name,
                    url=app.url,
                    domain=app.domain or sources.registrable_domain(app.url),
                    publisher_group=sources.publisher_group(app.url),
                    title=app.page_title,
                    snippet=f"Page with a {app.match_type} matching image (Cloud Vision web detection)"
                    + (f": {app.page_title}" if app.page_title else ""),
                    published_at=app.first_seen_at,
                    retrieval_method=RetrievalMethod.VISION_WEB_DETECTION,
                    tool="vision_web",
                    tool_version=tools.tool_versions().get("vision_web", ""),
                    branch="provenance",
                    is_fact_check=pub.is_fact_check,
                    is_government_source=pub.is_government,
                )
            )
    else:
        notes.append(
            "Cloud Vision web detection unconfigured (GOOGLE_VISION_API_KEY missing): "
            "own pHash/CLIP index only"
        )

    image_url = (resolved.oembed or {}).get("thumbnail_url")
    if tools.is_configured("factcheck") and image_url:
        res = await web.factcheck_image_search(tools, deps.settings, str(image_url))
        if res.ok:
            tools_used["factcheck"] = tools.tool_versions().get("factcheck", "")
            for review in web.parse_image_search(res.value)[:10]:
                items.append(
                    _review_item(
                        review, item_offset + len(items) + 1, tools.tool_versions().get("factcheck", "")
                    )
                )
                apps.append(
                    Appearance(
                        url=review.url,
                        page_title=review.title,
                        first_seen_at=parse_date(review.review_date),
                        timestamp_source="factcheck_review_date",
                        match_type="page",
                        domain=sources.registrable_domain(review.url),
                    )
                )
        elif res.error:
            errors.append(res.error)
    elif not tools.is_configured("factcheck"):
        notes.append("Fact Check imageSearch unconfigured (GOOGLE_FACTCHECK_API_KEY missing)")
    return items, apps


def _review_item(review: web.ImageReview, number: int, version: str) -> EvidenceItem:
    normalized, manipulation = _normalize_rating(review.rating_text)
    pub = sources.publisher_for(review.url)
    return EvidenceItem(
        id=f"P{number}",
        source_name=review.publisher or pub.name,
        url=review.url,
        domain=sources.registrable_domain(review.url),
        publisher_group=sources.publisher_group(review.url),
        title=review.title,
        snippet=_review_snippet(review),
        published_at=parse_date(review.review_date),
        retrieval_method=RetrievalMethod.FACTCHECK_IMAGESEARCH,
        tool="factcheck",
        tool_version=version,
        branch="provenance",
        is_fact_check=True,
        claimreview=ClaimReviewInfo(
            publisher=review.publisher or pub.name,
            rating_text=review.rating_text,
            normalized_rating=normalized,
            manipulation_rating=manipulation,
            claim_text=review.claim_text,
            claimant=review.claimant,
            similarity=0.0,  # image match, not a claim-text match: the fusion threshold must not count it
        ),
    )


def _review_snippet(review: web.ImageReview) -> str:
    about = review.claim_text or review.title or ""
    return f"Fact-check rating '{review.rating_text}' for an image match: {about}"[:600]


def _normalize_rating(text: str) -> tuple[float, bool]:
    try:
        from veriloop.services.evidence.lexicon import normalize_rating

        rating = normalize_rating(text)
        return float(rating.value), bool(rating.manipulation)
    except Exception:
        low = text.lower()
        manipulation = any(
            w in low for w in ("altered", "doctored", "deepfake", "edited", "manipulated", "morphed")
        )
        if any(
            w in low
            for w in (
                "false",
                "fake",
                "misleading",
                "incorrect",
                "pants on fire",
                *(["altered"] if manipulation else []),
            )
        ):
            return -1.0, manipulation
        if "true" in low or "correct" in low:
            return 1.0, manipulation
        return 0.0, manipulation


async def _date_pages(deps: Deps, apps: list[Appearance], errors: list[ToolError]) -> None:
    """article:published_time for the first few matching pages (first-seen needs timestamps)."""

    async def one(app: Appearance) -> None:
        res = await fetch_page(deps.tools, app.url)
        if not res.ok:
            return
        meta = parse_page_meta((res.value or {}).get("html") or "", app.url)
        when = parse_date(meta.get("published"))
        if when:
            app.first_seen_at = when
            app.timestamp_source = "article:published_time"
        if not app.page_title and meta.get("title"):
            app.page_title = meta["title"]

    await asyncio.gather(*(one(a) for a in apps[:MAX_PAGE_DATES]))
