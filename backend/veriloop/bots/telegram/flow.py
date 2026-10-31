"""Telegram message -> verification job -> reply. Transport-agnostic so it is testable without Telegram.

On a message with a link, text, photo or video: reply "Checking...", submit a job (source=telegram,
expires_at = now + TELEGRAM_TTL_HOURS, chat ref = HMAC), keep the raw content only in ephemeral_inputs,
wait for the run, then edit the reply with the verdict, the top 2 sources and the audit link, and send
the report card image. Replies are plain text (no parse mode), so user/tool text needs no escaping.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog

from veriloop.contracts import JobStatus, JobView, VerdictLabel, VerifyRequest, utcnow
from veriloop.core.ids import new_job_id
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.reporting import links, render_report_card

from .privacy import chat_hmac

log = structlog.get_logger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
MAX_TEXT = 4000
WAIT_TIMEOUT_S = 300.0
HELP_TEXT = (
    "Send me a link to a post or video, a forwarded message, a photo or a short video, and I will check it "
    "against fact-checks, news and forensic models. I reply with a cited verdict or say honestly when the "
    "evidence is not enough.\n\nPrivacy: your message is deleted after {ttl} hours; your chat id is never "
    "stored. Nothing is ever reported on your behalf."
)
REASONS = {
    "NO_EVIDENCE": "no relevant evidence was found (this is NOT the same as false)",
    "SINGLE_SOURCE": "fewer than two independent sources",
    "CONFLICT": "independent sources conflict",
    "LOW_CONFIDENCE": "the evidence is not strong enough",
    "FORENSIC_INCONCLUSIVE": "the forensic analysis is inconclusive",
    "UNGROUNDED_RATIONALE": "the explanation could not be grounded in the evidence",
    "JUDGE_DISAGREEMENT": "the reviewing models disagreed",
    "TOOL_OUTAGE": "some checking tools were unavailable",
    "LANGUAGE_UNSUPPORTED": "the language is not supported well enough",
}
MediaKind = Literal["photo", "video", "document"]


@dataclass
class IncomingMessage:
    chat_id: int
    text: str | None = None  # message text or media caption
    media_kind: MediaKind | None = None
    media_ext: str = ".bin"
    download: Callable[[Path], Awaitable[None]] | None = None  # writes the media to the given path


class Replier(Protocol):
    async def reply(self, text: str) -> Any: ...
    async def edit(self, handle: Any, text: str) -> None: ...
    async def send_photo(self, png: bytes, caption: str) -> None: ...


class SubmitFn(Protocol):
    async def __call__(
        self,
        request: VerifyRequest,
        upload_path: str | None = None,
        *,
        job_id: str | None = None,
        source_ref_hmac: str | None = None,
        expires_at: Any = None,
    ) -> str: ...


WaitFn = Callable[[str, float], Awaitable[JobView | None]]


def parse_message(text: str | None) -> tuple[str | None, str | None]:
    """(first URL, remaining text) from a message; either may be None."""
    if not text or not text.strip():
        return None, None
    text = text.strip()[:MAX_TEXT]
    match = URL_RE.search(text)
    url = match.group(0).rstrip(".,);]") if match else None
    rest = (text.replace(url, " ", 1) if url else text).strip()
    return url, (" ".join(rest.split()) or None)


def format_result(view: JobView, settings: Any) -> str:
    """The verdict message: label, confidence, why, top 2 sources, audit link."""
    if view.status is JobStatus.FAILED or view.verdict is None:
        return (
            "Sorry, this check could not be completed"
            + (f" ({view.error})" if view.error else "")
            + f".\nDetails: {links.job_page_url(settings, view.job_id)}"
        )
    label = view.verdict
    lines: list[str] = []
    if label in (VerdictLabel.TRUE, VerdictLabel.FALSE, VerdictLabel.MANIPULATED):
        conf = f" ({view.confidence:.0%} confidence)" if view.confidence is not None else ""
        lines.append(f"VERDICT: {label.value}{conf}")
        what = {
            VerdictLabel.TRUE: "Supported",
            VerdictLabel.FALSE: "Refuted",
            VerdictLabel.MANIPULATED: "Manipulation documented",
        }[label]
        lines.append(f"{what} by {view.independent_source_count} independent sources.")
    elif label is VerdictLabel.UNVERIFIABLE:
        reason = REASONS.get(view.abstain_reason.value, "") if view.abstain_reason else ""
        lines.append("VERDICT: UNVERIFIABLE (VeriLoop abstains)")
        if reason:
            lines.append(f"Why: {reason}.")
    else:
        lines.append("NOT CHECKABLE: no factual claim to verify.")
        if view.abstain_detail:
            lines.append(view.abstain_detail)
    cited = [s for s in view.sources if s.id in set(view.cited_evidence_ids)] or view.sources
    if cited:
        lines.append("")
        lines.append("Top sources:")
        for i, s in enumerate(cited[:2], 1):
            lines.append(f"{i}. {s.source_name}: {(s.title or s.snippet)[:140]}")
            lines.append(f"   {s.url}")
    if view.degraded_tools:
        lines.append("")
        lines.append("Some tools were unavailable: " + ", ".join(view.degraded_tools))
    lines.append("")
    lines.append(f"Signed audit record: {links.audit_page_url(settings, view.job_id)}")
    lines.append(f"Full report: {links.job_page_url(settings, view.job_id)}")
    return "\n".join(lines)


class TelegramFlow:
    def __init__(
        self, deps: Deps, submit: SubmitFn, wait: WaitFn, *, timeout_s: float = WAIT_TIMEOUT_S
    ) -> None:
        self.deps = deps
        self.submit = submit
        self.wait = wait
        self.timeout_s = timeout_s

    @property
    def ttl_s(self) -> float:
        return float(self.deps.settings.telegram_ttl_hours) * 3600.0

    def help_text(self) -> str:
        return HELP_TEXT.format(ttl=self.deps.settings.telegram_ttl_hours)

    async def handle(self, msg: IncomingMessage, out: Replier) -> str | None:
        """Process one message end to end. Returns the job id (None when nothing was checkable)."""
        url, text = parse_message(msg.text)
        if not (url or text or msg.download):
            await out.reply(self.help_text())
            return None
        handle = await out.reply("Checking... this usually takes 10-60 seconds. I will update this message.")
        job_id = new_job_id()
        settings = self.deps.settings
        upload_path: str | None = None
        try:
            if msg.download is not None:
                dest = settings.media_dir(job_id) / f"upload{msg.media_ext}"
                await msg.download(dest)
                upload_path = str(dest)
            request = VerifyRequest(
                url=url,
                text=text,
                source="telegram",
                locale="en-IN",
                upload_id=job_id if upload_path else None,
            )
            expires_at = utcnow() + timedelta(seconds=self.ttl_s)
            job_id = await self.submit(
                request,
                upload_path,
                job_id=job_id,
                source_ref_hmac=chat_hmac(settings, msg.chat_id),
                expires_at=expires_at,
            )
            await self._store_ephemeral(job_id, url, text, upload_path)
            log.info(
                "telegram_job_submitted",
                job_id=job_id,
                has_url=bool(url),
                has_text=bool(text),
                media=msg.media_kind,
            )
            try:
                view = await self.wait(job_id, self.timeout_s)
            except TimeoutError:
                await out.edit(
                    handle, "Still checking. Follow progress here: " + links.job_page_url(settings, job_id)
                )
                return job_id
            if view is None:
                await out.edit(handle, "Sorry, the check was lost. Please send it again.")
                return job_id
            await out.edit(handle, format_result(view, settings))
            if view.status is JobStatus.COMPLETED and view.verdict is not None:
                card = await asyncio.to_thread(render_report_card, view, settings)
                await out.send_photo(card, "Report card: scan the QR code to check the signed audit record.")
            return job_id
        except Exception as exc:
            log.exception("telegram_handle_failed", job_id=job_id)
            await out.edit(
                handle, f"Sorry, something went wrong ({type(exc).__name__}). Please try again later."
            )
            return None

    async def _store_ephemeral(
        self, job_id: str, url: str | None, text: str | None, upload: str | None
    ) -> None:
        # D7/G6: the only place raw message content is kept, and only until the TTL.
        for kind, content in (("telegram_url", url), ("telegram_text", text), ("upload_path", upload)):
            if content:
                await repo.put_ephemeral(
                    self.deps.db, kind=kind, content=content, ttl_s=self.ttl_s, job_id=job_id
                )
