"""Shareable report card PNG (SPEC §7 Telegram/WhatsApp): verdict, claim, top 2 sources, QR to the audit page.

1080x1350 (4:5, renders well in chat apps). Fonts: Nirmala UI on Windows (Latin + Indic scripts),
DejaVu on Linux, Pillow's built-in font otherwise; Pillow's raqm layout shapes Devanagari when the
library is available. All text comes from the JobView; the QR encodes APP_BASE_URL/a/<job_id>.
"""

from __future__ import annotations

import io
import threading
from functools import lru_cache
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

from veriloop.contracts import JobView, VerdictLabel
from veriloop.core.config import Settings

from . import links

W, H = 1080, 1350
PAD = 64
COLORS = {
    VerdictLabel.TRUE: (27, 127, 59),
    VerdictLabel.FALSE: (180, 35, 24),
    VerdictLabel.MANIPULATED: (122, 31, 162),
    VerdictLabel.UNVERIFIABLE: (91, 100, 114),
    VerdictLabel.NOT_CHECKABLE: (91, 100, 114),
}
HEADLINES = {
    VerdictLabel.TRUE: "Verified as true",
    VerdictLabel.FALSE: "False: refuted by independent sources",
    VerdictLabel.MANIPULATED: "Manipulated / synthetic media",
    VerdictLabel.UNVERIFIABLE: "Unverifiable: not enough evidence (this is NOT 'false')",
    VerdictLabel.NOT_CHECKABLE: "No checkable claim",
}
INK = (16, 24, 40)
MUTED = (71, 84, 103)
BG = (246, 247, 249)

_REGULAR = (
    ("C:/Windows/Fonts/Nirmala.ttc", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ("C:/Windows/Fonts/segoeui.ttf", 0),
    ("C:/Windows/Fonts/arial.ttf", 0),
)
_BOLD = (
    ("C:/Windows/Fonts/Nirmala.ttc", 1),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 0),
    ("C:/Windows/Fonts/segoeuib.ttf", 0),
    ("C:/Windows/Fonts/arialbd.ttf", 0),
)
_lock = threading.Lock()

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


@lru_cache(maxsize=32)
def _font(size: int, bold: bool = False) -> Font:
    with _lock:
        for path, index in _BOLD if bold else _REGULAR:
            if Path(path).is_file():
                try:
                    return ImageFont.truetype(path, size, index=index)
                except OSError:
                    continue
        return ImageFont.load_default(size=size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Font, width: int, max_lines: int) -> list[str]:
    words = " ".join(text.split()).split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
        while draw.textlength(current, font=font) > width and len(current) > 1:  # very long tokens (URLs)
            cut = len(current)
            while cut > 1 and draw.textlength(current[:cut], font=font) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines or (len(lines) == max_lines and " ".join(lines) != " ".join(words)):
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _qr(url: str, size: int) -> Image.Image:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    return img.resize((size, size), Image.Resampling.NEAREST)


def render_report_card(job: JobView, settings: Settings) -> bytes:
    label = job.verdict or VerdictLabel.UNVERIFIABLE
    color = COLORS.get(label, (52, 64, 84))
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    inner = W - 2 * PAD

    # header band
    draw.rectangle((0, 0, W, 300), fill=color)
    draw.text((PAD, 52), "VERILOOP VERDICT", font=_font(30, True), fill=(255, 255, 255))
    draw.text((PAD, 96), label.value.replace("_", " "), font=_font(104, True), fill=(255, 255, 255))
    sub = HEADLINES.get(label, "")
    if job.confidence is not None and label in (
        VerdictLabel.TRUE,
        VerdictLabel.FALSE,
        VerdictLabel.MANIPULATED,
    ):
        sub = f"{sub} · {job.confidence:.0%} confidence"
    draw.text((PAD, 236), _wrap(draw, sub, _font(32), inner, 1)[0], font=_font(32), fill=(255, 255, 255))

    y = 340
    draw.text((PAD, y), "CLAIM", font=_font(28, True), fill=MUTED)
    y += 44
    claim = job.claim.primary_claim() if job.claim and not job.redacted else None
    claim_text = (
        f"“{claim.text}”" if claim else "Claim text removed after the privacy TTL." if job.redacted else "n/a"
    )
    for line in _wrap(draw, claim_text, _font(40), inner, 4):
        draw.text((PAD, y), line, font=_font(40), fill=INK)
        y += 54

    y += 24
    cited = [s for s in job.sources if s.id in set(job.cited_evidence_ids)] or job.sources
    draw.text(
        (PAD, y),
        f"TOP SOURCES ({job.independent_source_count} independent)",
        font=_font(28, True),
        fill=MUTED,
    )
    y += 46
    if not cited:
        draw.text((PAD, y), "No evidence found (this is not the same as false).", font=_font(32), fill=INK)
        y += 48
    for s in cited[:2]:
        date = f" · {s.published_at.date().isoformat()}" if s.published_at else ""
        draw.text((PAD, y), f"{s.source_name}{date}", font=_font(32, True), fill=INK)
        y += 44
        for line in _wrap(draw, s.title or s.snippet, _font(30), inner, 2):
            draw.text((PAD, y), line, font=_font(30), fill=INK)
            y += 40
        draw.text((PAD, y), s.domain, font=_font(26), fill=MUTED)
        y += 52

    # footer: QR to the audit page
    qr_size = 260
    top = H - qr_size - PAD
    draw.line((PAD, top - 30, W - PAD, top - 30), fill=(208, 213, 221), width=2)
    img.paste(_qr(links.audit_page_url(settings, job.job_id), qr_size), (W - PAD - qr_size, top))
    text_w = inner - qr_size - 32
    fy = top + 6
    for line in _wrap(
        draw, "Scan to check the signed, tamper-evident audit record", _font(34, True), text_w, 2
    ):
        draw.text((PAD, fy), line, font=_font(34, True), fill=INK)
        fy += 46
    for line in _wrap(draw, links.audit_page_url(settings, job.job_id), _font(26), text_w, 2):
        draw.text((PAD, fy), line, font=_font(26), fill=MUTED)
        fy += 36
    audit = f"audit #{job.audit_id}" if job.audit_id is not None else "audit pending"
    draw.text((PAD, H - PAD - 30), f"Job {job.job_id} · {audit} · {job.mode}", font=_font(24), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
