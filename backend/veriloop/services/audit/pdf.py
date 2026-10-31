"""Signed audit PDF (SPEC §9): verdict, evidence, forensic scores, models/tools, integrity block, QR.

Everything printed comes from the *signed* payload (so the PDF shows what the chain attests), with
the JobView only adding display niceties (source titles). The appendix prints the exact canonical
payload text that was hashed. A Unicode TTF is registered when one is available (Nirmala UI on
Windows, DejaVu/Noto on Linux) so Hindi/Telugu claims do not render as empty boxes; reportlab does not
shape complex scripts, so conjuncts may look unjoined, which is acceptable for an audit printout.
"""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import structlog
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from veriloop.contracts import AuditEntryView, JobView
from veriloop.core.config import Settings

from .keys import ALGORITHM, public_key_pem

log = structlog.get_logger(__name__)

_FONT_CANDIDATES: tuple[tuple[str, str, int], ...] = (
    ("VLSans", "C:/Windows/Fonts/Nirmala.ttc", 0),
    ("VLSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 0),
    ("VLSans", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", 0),
    ("VLSans", "C:/Windows/Fonts/arial.ttf", 0),
)
_MONO_CANDIDATES: tuple[tuple[str, str, int], ...] = (
    ("VLMono", "C:/Windows/Fonts/consola.ttf", 0),
    ("VLMono", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),
)
_fonts: dict[str, str] = {}
_font_lock = threading.Lock()

VERDICT_COLORS = {
    "TRUE": colors.HexColor("#1b7f3b"),
    "FALSE": colors.HexColor("#b42318"),
    "MANIPULATED": colors.HexColor("#7a1fa2"),
    "UNVERIFIABLE": colors.HexColor("#5b6472"),
    "NOT_CHECKABLE": colors.HexColor("#5b6472"),
}


def _register(candidates: tuple[tuple[str, str, int], ...], fallback: str) -> str:
    for name, path, index in candidates:
        if not Path(path).is_file():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=index))
            return name
        except Exception as exc:  # a broken/unsupported font file must not break the PDF
            log.info("pdf_font_unavailable", path=path, error=str(exc))
    return fallback


def fonts() -> tuple[str, str]:
    """(body font, mono font) names, registered once per process."""
    with _font_lock:
        if not _fonts:
            _fonts["sans"] = _register(_FONT_CANDIDATES, "Helvetica")
            _fonts["mono"] = _register(_MONO_CANDIDATES, "Courier")
    return _fonts["sans"], _fonts["mono"]


def audit_page_url(settings: Settings, job_id: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/a/{job_id}"


def _styles() -> dict[str, ParagraphStyle]:
    sans, mono = fonts()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "t", parent=base["Title"], fontName=sans, fontSize=17, leading=21, alignment=TA_LEFT
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName=sans, fontSize=12, leading=15, spaceBefore=8
        ),
        "body": ParagraphStyle("b", parent=base["BodyText"], fontName=sans, fontSize=9, leading=12),
        "small": ParagraphStyle("s", parent=base["BodyText"], fontName=sans, fontSize=7.5, leading=9.5),
        "mono": ParagraphStyle(
            "m", parent=base["Code"], fontName=mono, fontSize=6.5, leading=8, splitLongWords=1
        ),
        "verdict": ParagraphStyle(
            "v",
            parent=base["Title"],
            fontName=sans,
            fontSize=20,
            leading=24,
            textColor=colors.white,
            alignment=TA_LEFT,
        ),
    }


def _p(text: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape("" if text is None else str(text)), style)


def _kv_table(
    rows: list[tuple[str, Any]], st: dict[str, ParagraphStyle], widths: tuple[float, float]
) -> Table:
    data = [[_p(k, st["small"]), _p(v, st["small"])] for k, v in rows]
    table = Table(data, colWidths=widths)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d5dd")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f4f7")),
            ]
        )
    )
    return table


def _grid(
    header: list[str], rows: list[list[Any]], st: dict[str, ParagraphStyle], widths: list[float]
) -> Table:
    data = [[_p(h, st["small"]) for h in header]] + [[_p(c, st["small"]) for c in r] for r in rows]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e4e7ec")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d5dd")),
            ]
        )
    )
    return table


def _qr(url: str, size: float = 32 * mm) -> Drawing:
    widget = QrCodeWidget(url)
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    return drawing


def _fmt_conf(value: Any) -> str:
    return f"{float(value):.0%}" if isinstance(value, int | float) else "n/a"


def render_pdf(entry: AuditEntryView, job: JobView | None, settings: Settings) -> bytes:
    """Render the signed audit record for one chain entry as PDF bytes."""
    st = _styles()
    payload: dict[str, Any] = json.loads(entry.payload_json)
    verdict = payload.get("verdict") or {}
    width = A4[0] - 30 * mm
    story: list[Any] = []

    story.append(_p("VeriLoop signed audit record", st["title"]))
    story.append(
        _p(
            f"Job {entry.job_id} · chain entry #{entry.id} · event {entry.event_type.value} · "
            f"recorded {entry.created_at.isoformat()} · mode {payload.get('mode', 'n/a')}",
            st["small"],
        )
    )
    story.append(Spacer(1, 4 * mm))

    label = verdict.get("label") or entry.event_type.value.upper()
    band = Table(
        [[_p(label, st["verdict"]), _p(f"confidence {_fmt_conf(verdict.get('confidence'))}", st["verdict"])]],
        colWidths=[width * 0.6, width * 0.4],
    )
    band.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), VERDICT_COLORS.get(str(label), colors.HexColor("#344054"))),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(band)
    story.append(Spacer(1, 2 * mm))

    claim = payload.get("claim") or {}
    summary_rows: list[tuple[str, Any]] = [
        (
            "Claim",
            claim.get("text")
            or (f"sha256 {claim['text_sha256']} (content not stored)" if claim.get("text_sha256") else "n/a"),
        ),
        ("Evidence state", verdict.get("evidence_state")),
        ("Abstain reason", verdict.get("abstain_reason")),
        (
            "Detail",
            verdict.get("abstain_detail")
            or (
                f"sha256 {verdict['abstain_detail_sha256']} (content not stored)"
                if verdict.get("abstain_detail_sha256")
                else None
            ),
        ),
        ("Independent sources", verdict.get("independent_source_count")),
        (
            "Report eligible / legal eligible",
            f"{verdict.get('report_eligible')} / {verdict.get('legal_eligible')}",
        ),
        ("Labels", "; ".join(payload.get("labels") or []) or "none"),
    ]
    if entry.event_type.value != "verdict":
        summary_rows = [
            (k, v) for k, v in payload.items() if k not in ("tool_versions", "resolved_model_ids")
        ]
    story.append(_kv_table(summary_rows, st, (40 * mm, width - 40 * mm)))

    rationale = verdict.get("rationale") or []
    if rationale:
        story.append(_p("Rationale (every sentence cites stored evidence)", st["h2"]))
        for r in rationale:
            text = r.get("text") or f"sha256 {r.get('text_sha256')}"
            story.append(_p(f"• {text} [{', '.join(r.get('evidence_ids') or [])}]", st["body"]))

    titles = {s.id: s.title for s in job.sources} if job else {}
    evidence = payload.get("evidence") or []
    if evidence:
        story.append(_p("Evidence (cited first)", st["h2"]))
        ordered = sorted(evidence, key=lambda e: not e.get("cited"))
        rows = [
            [
                e.get("id"),
                f"{e.get('source_name') or e.get('domain')}"
                + (f" — {titles[e['id']]}" if titles.get(e.get("id")) else ""),
                (e.get("published_at") or "")[:10],
                e.get("retrieval_method"),
                e.get("stance"),
                e.get("url"),
            ]
            for e in ordered
        ]
        story.append(
            _grid(
                ["ID", "Source", "Published", "Method", "Stance", "URL"],
                rows,
                st,
                [10 * mm, 45 * mm, 18 * mm, 25 * mm, 15 * mm, width - 113 * mm],
            )
        )

    forensic = payload.get("forensic")
    if forensic:
        story.append(
            _p(
                f"Forensics: {forensic.get('status')} (ensemble P(fake) {forensic.get('ensemble_score')}, "
                f"disagreement {forensic.get('disagreement')}, abstain band {forensic.get('band')})",
                st["h2"],
            )
        )
        models = forensic.get("models") or []
        if models:
            story.append(
                _grid(
                    ["Model", "Provider", "Raw P(fake)", "Weight", "Frames", "Error"],
                    [
                        [
                            m.get("model_id"),
                            m.get("provider"),
                            m.get("raw_score"),
                            m.get("weight"),
                            m.get("frames_scored"),
                            m.get("error"),
                        ]
                        for m in models
                    ],
                    st,
                    [60 * mm, 22 * mm, 20 * mm, 15 * mm, 15 * mm, width - 132 * mm],
                )
            )
    hashes = payload.get("media_hashes") or []
    if hashes:
        story.append(_p("Media hashes", st["h2"]))
        story.append(
            _grid(
                ["ID", "Kind", "SHA-256", "pHash"],
                [[h.get("id"), h.get("kind"), h.get("sha256"), h.get("phash")] for h in hashes],
                st,
                [15 * mm, 22 * mm, width - 67 * mm, 30 * mm],
            )
        )

    story.append(_p("Models, tools, weights and thresholds", st["h2"]))
    mt_rows: list[tuple[str, Any]] = [
        (f"model {k}", v) for k, v in (payload.get("resolved_model_ids") or {}).items()
    ]
    mt_rows += [(f"tool {k}", v) for k, v in sorted((payload.get("tool_versions") or {}).items())]
    mt_rows += [(f"weight {k}", v) for k, v in (payload.get("weights") or {}).items()]
    mt_rows += [(k, v) for k, v in (payload.get("thresholds") or {}).items()]
    mt_rows.append(("calibration version", payload.get("calibration_version")))
    mt_rows.append(("weights version", payload.get("weights_version")))
    story.append(_kv_table(mt_rows, st, (45 * mm, width - 45 * mm)))

    url = audit_page_url(settings, entry.job_id)
    integrity = _kv_table(
        [
            ("Payload SHA-256", entry.payload_hash),
            ("Previous hash", entry.prev_hash),
            (f"Signature ({ALGORITHM}, base64)", entry.signature),
            ("Signed message", "<prev_hash>:<payload_hash>"),
            ("Key fingerprint (sha256 of SPKI DER)", entry.key_fingerprint),
            ("Public key", public_key_pem(settings).strip()),
            ("Verify online", f"{settings.app_base_url.rstrip('/')}/audit/{entry.job_id}/verify"),
        ],
        st,
        (45 * mm, width - 45 * mm - 36 * mm),
    )
    qr_block = Table([[integrity, [_qr(url), _p(url, st["small"])]]], colWidths=[width - 36 * mm, 36 * mm])
    qr_block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(KeepTogether([_p("Integrity", st["h2"]), qr_block]))

    story.append(
        _p(
            "Appendix: canonical payload (the exact text that was hashed; line breaks are for display)",
            st["h2"],
        )
    )
    story.append(_p(entry.payload_json, st["mono"]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"VeriLoop audit {entry.job_id}",
        author="VeriLoop",
        subject=f"payload sha256 {entry.payload_hash}",
    )
    doc.build(story)
    return buf.getvalue()
