"""Audit service (BUILD_SPEC §5, §8): hash-chained, Ed25519-signed, append-only event log + signed PDF."""

from .chain import (
    GENESIS_HASH,
    append_event,
    get_entries,
    get_entry,
    get_entry_by_id,
    verify_chain,
)
from .keys import derive_secret, load_signing_key, public_key_pem
from .payload import build_verdict_payload
from .pdf import audit_page_url, render_pdf

__all__ = [
    "GENESIS_HASH",
    "append_event",
    "audit_page_url",
    "build_verdict_payload",
    "derive_secret",
    "get_entries",
    "get_entry",
    "get_entry_by_id",
    "load_signing_key",
    "public_key_pem",
    "render_pdf",
    "verify_chain",
]
