"""Legal escalation service (SPEC §8, BUILD_SPEC §5/§7 rule 8): eligibility + drafts (never sent)."""

from .drafting import DISCLAIMER, extract_placeholders, generate_legal_draft, load_template
from .eligibility import (
    legal_eligibility,
    legal_eligibility_parts,
    report_eligibility,
    report_eligibility_parts,
)

__all__ = [
    "DISCLAIMER",
    "extract_placeholders",
    "generate_legal_draft",
    "legal_eligibility",
    "legal_eligibility_parts",
    "load_template",
    "report_eligibility",
    "report_eligibility_parts",
]
