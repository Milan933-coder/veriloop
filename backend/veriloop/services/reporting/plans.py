"""Per-platform report plans + the exact text a person will send (SPEC §7, D-10, G5, D4).

method:
  playwright_guided  X / Instagram / Facebook / YouTube: the presenter-side reporter-agent opens the
                     post and the report dialog and pre-fills the text, then STOPS before submit.
  report_card        Telegram / WhatsApp: no report API, so a shareable card + forward instructions.
  deeplink_guided    anything else: open the link and follow the steps by hand.
Every plan is advisory: VeriLoop never submits a report.
"""

from __future__ import annotations

from veriloop.contracts import JobView, PlatformPlan, VerdictLabel
from veriloop.core.config import Settings

from . import links

GUIDED = ("x", "instagram", "facebook", "youtube")
CARD = ("telegram", "whatsapp")
DISPLAY = {
    "x": "X",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "tiktok": "TikTok",
}
SUBMIT_YOURSELF = "Review everything, then press Submit yourself. VeriLoop never submits a report."
MAX_PREFILL = 900


def _short(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def report_category(platform: str, label: VerdictLabel | None) -> str:
    manipulated = label is VerdictLabel.MANIPULATED
    if platform == "x":
        return "Synthetic or manipulated media" if manipulated else "Misleading information"
    if platform in ("instagram", "facebook"):
        return "False information (altered / AI-generated media)" if manipulated else "False information"
    if platform == "youtube":
        return "Misinformation (manipulated media)" if manipulated else "Misinformation"
    return "Manipulated media" if manipulated else "False or misleading information"


def _verdict_phrase(job: JobView) -> str:
    conf = f"{job.confidence:.0%} confidence" if job.confidence is not None else "confidence n/a"
    if job.verdict is VerdictLabel.MANIPULATED:
        what = "the media is manipulated / synthetic"
    elif job.verdict is VerdictLabel.FALSE:
        what = "independent sources refute this claim"
    else:
        what = job.evidence_state.value if job.evidence_state else "see sources"
    return f"{job.verdict.value if job.verdict else 'n/a'} ({conf}): {what}"


def _cited(job: JobView, n: int) -> list:
    cited = [s for s in job.sources if s.id in set(job.cited_evidence_ids)]
    return (cited or job.sources)[:n]


def preview_text(job: JobView, settings: Settings) -> str:
    """Exactly the text the person reviews before confirming (G5)."""
    lines = ["VeriLoop verification report", "", f"Verdict: {_verdict_phrase(job)}"]
    claim = None if job.redacted or job.claim is None else (job.claim.primary_claim() or None)
    if claim is not None:
        lines.append(f'Claim: "{_short(claim.text, 280)}"')
    if job.input and job.input.url:
        lines.append(f"Content: {job.input.url}")
    sources = _cited(job, 5)
    lines += ["", f"Independent sources ({job.independent_source_count}):"]
    for i, s in enumerate(sources, 1):
        date = f" ({s.published_at.date().isoformat()})" if s.published_at else ""
        lines.append(f"{i}. {s.source_name}{date}: {_short(s.title or s.snippet, 120)} - {s.url}")
    if job.forensic is not None and job.verdict is VerdictLabel.MANIPULATED:
        score = (
            f", P(fake) {job.forensic.ensemble_score:.0%}" if job.forensic.ensemble_score is not None else ""
        )
        lines.append(f"Forensic analysis: {job.forensic.status.value}{score}")
    lines += [
        "",
        f"Signed audit record: {links.audit_pdf_url(settings, job.job_id)}",
        f"Verify it independently: {links.audit_page_url(settings, job.job_id)}",
        "",
        "Prepared with VeriLoop and reviewed by a person before reporting.",
    ]
    return "\n".join(lines)


def prefilled_text(job: JobView, settings: Settings) -> str:
    """Compact version for report dialogs with small free-text boxes."""
    sources = ", ".join(s.url for s in _cited(job, 2))
    text = (
        f"Verified {_verdict_phrase(job)}. Sources: {sources}. "
        f"Signed audit record: {links.audit_page_url(settings, job.job_id)}"
    )
    return _short(text, MAX_PREFILL)


def _guided_steps(platform: str, url: str | None, category: str) -> list[str]:
    open_step = f"Open the post: {url}" if url else "Open the post in the app or browser"
    if platform == "x":
        mid = ["Click the ... (More) menu on the post", "Choose 'Report post'"]
    elif platform == "instagram":
        mid = ["Tap ... (More) on the post", "Tap 'Report'"]
    elif platform == "facebook":
        mid = ["Click ... (More) on the post", "Choose 'Report post'"]
    else:  # youtube
        mid = ["Click ... (More) under the video", "Choose 'Report'"]
    return [
        open_step,
        *mid,
        f"Pick the category closest to '{category}'",
        "Where a free-text box is offered, paste the prefilled text (it links the signed evidence)",
        SUBMIT_YOURSELF,
    ]


def _card_steps(platform: str) -> list[str]:
    if platform == "whatsapp":
        return [
            "Download the report card image (card.png)",
            "Open the chat or group where the message was shared",
            "Long-press the message, choose Reply, attach the card and paste the text below",
            "Ask people not to forward the original; the card's QR code lets anyone check the signed "
            "audit record",
            "If someone is spreading it deliberately, you can also use Report in the contact or group info",
            "Nothing is sent automatically: you choose where to share it",
        ]
    return [
        "Download the report card image (card.png)",
        "In the chat or channel where you saw the post, reply to it with the card and the text below",
        "For public channels you can also use the ... menu, then Report",
        "The card's QR code lets anyone check the signed audit record",
        "Nothing is sent automatically: you choose where to share it",
    ]


def build_plans(job: JobView, settings: Settings) -> list[PlatformPlan]:
    """Primary platform plan first (when the content came from one), then the shareable cards."""
    platform = job.input.platform.value if job.input else "text"
    url = job.input.url if job.input else None
    card = links.card_url(settings, job.job_id)
    prefill = prefilled_text(job, settings)
    full = preview_text(job, settings)
    plans: list[PlatformPlan] = []
    if platform in GUIDED:
        category = report_category(platform, job.verdict)
        plans.append(
            PlatformPlan(
                platform=platform,
                target_url=url,
                method="playwright_guided" if url else "deeplink_guided",
                evidence_attached=True,
                steps=_guided_steps(platform, url, category),
                prefilled_text=prefill,
                report_category=category,
                card_url=card,
            )
        )
    elif platform not in CARD and url:
        plans.append(
            PlatformPlan(
                platform=platform,
                target_url=url,
                method="deeplink_guided",
                evidence_attached=True,
                steps=[
                    f"Open the page: {url}",
                    "Use the site's own report / feedback / contact link",
                    "Paste the prefilled text and attach the evidence bundle (bundle.zip)",
                    SUBMIT_YOURSELF,
                ],
                prefilled_text=prefill,
                report_category=report_category(platform, job.verdict),
                card_url=card,
            )
        )
    for name in CARD:
        plans.append(
            PlatformPlan(
                platform=name,
                target_url=url if platform == name else None,
                method="report_card",
                evidence_attached=True,
                steps=_card_steps(name),
                prefilled_text=full,
                report_category=None,
                card_url=card,
            )
        )
    if platform in CARD:  # the source platform's card goes first
        plans.sort(key=lambda p: p.platform != platform)
    return plans


def display_name(platform: str) -> str:
    return DISPLAY.get(platform, platform)
