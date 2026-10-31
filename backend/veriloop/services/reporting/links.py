"""Absolute links into this deployment (APP_BASE_URL) used by bundles, cards, plans and legal drafts."""

from __future__ import annotations

from urllib.parse import quote

from veriloop.core.config import Settings


def base(settings: Settings) -> str:
    return settings.app_base_url.rstrip("/")


def _id(job_id: str) -> str:
    return quote(job_id, safe="")


def audit_page_url(settings: Settings, job_id: str) -> str:
    """SPA audit page (QR codes point here): shows the entry and verifies it in the browser."""
    return f"{base(settings)}/a/{_id(job_id)}"


def audit_pdf_url(settings: Settings, job_id: str) -> str:
    return f"{base(settings)}/audit/{_id(job_id)}/pdf"


def audit_verify_url(settings: Settings, job_id: str) -> str:
    return f"{base(settings)}/audit/{_id(job_id)}/verify"


def pubkey_url(settings: Settings) -> str:
    return f"{base(settings)}/audit/pubkey"


def bundle_url(settings: Settings, job_id: str) -> str:
    return f"{base(settings)}/report/{_id(job_id)}/bundle.zip"


def card_url(settings: Settings, job_id: str) -> str:
    return f"{base(settings)}/report/{_id(job_id)}/card.png"


def job_page_url(settings: Settings, job_id: str) -> str:
    return f"{base(settings)}/j/{_id(job_id)}"
