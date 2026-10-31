"""HTTP request/response models. The middleware adds `request_id` and `latency_ms` to every JSON body."""

from datetime import datetime
from typing import Literal

from pydantic import model_validator

from .analysis import ForensicResult, ProvenanceResult
from .common import Contract, NodeTiming, SamplingInfo, ToolError
from .enums import (
    AbstainReason,
    AuditEventType,
    EvidenceState,
    JobStatus,
    Platform,
    ToolState,
    VerdictLabel,
)
from .evidence import EvidenceItem
from .media import ClaimExtraction
from .reasoning import DebateResult, FusionResult, RationaleSentence

Source = Literal["web", "pwa", "telegram", "api", "extension"]


class VerifyRequest(Contract):
    url: str | None = None
    text: str | None = None
    platform: Platform | None = None  # auto-detected when omitted
    locale: str = "en-IN"
    source: Source = "web"
    upload_id: str | None = None  # set by the API when a file is uploaded via multipart

    @model_validator(mode="after")
    def _one_input(self) -> "VerifyRequest":
        if not (self.url or self.text or self.upload_id):
            raise ValueError("one of url, text or upload is required")
        return self


class VerifyAccepted(Contract):
    job_id: str
    status: JobStatus
    status_url: str
    events_url: str
    queue_position: int = 0


class InputSummary(Contract):
    platform: Platform
    input_kind: str
    url: str | None = None  # None after Telegram TTL redaction
    title: str | None = None
    media_accessible: bool = False
    media_count: int = 0
    sampling: SamplingInfo | None = None
    fetch_methods: list[str] = []
    labels: list[str] = []
    media_hashes: list[dict] = []  # [{id, kind, sha256, phash, frame_ts}]


class JobView(Contract):
    """GET /verify/{job_id}. Also persisted as jobs.result_json."""

    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime | None = None
    source: Source = "web"
    mode: str = "live"  # live|record|replay
    verdict: VerdictLabel | None = None
    confidence: float | None = None
    abstain_reason: AbstainReason | None = None
    abstain_detail: str | None = None
    evidence_state: EvidenceState | None = None
    rationale: list[RationaleSentence] = []
    cited_evidence_ids: list[str] = []
    independent_source_count: int = 0
    sources: list[EvidenceItem] = []  # cited first, then the rest
    forensic: ForensicResult | None = None
    provenance: ProvenanceResult | None = None
    claim: ClaimExtraction | None = None  # redacted after Telegram TTL
    input: InputSummary | None = None
    fusion: FusionResult | None = None
    debate: DebateResult | None = None
    trace: list[NodeTiming] = []
    tool_errors: list[ToolError] = []
    degraded_tools: list[str] = []
    labels: list[str] = []
    report_eligible: bool = False
    legal_eligible: bool = False
    legal_block_reason: str | None = None
    audit_id: int | None = None
    resolved_models: dict[str, str] = {}
    queue_position: int | None = None
    status_detail: str | None = None  # e.g. "processing (rate-limited: serper)"
    redacted: bool = False
    error: str | None = None


# ---- Reporting ---------------------------------------------------------------------------

ReportMethod = Literal["playwright_guided", "report_card", "deeplink_guided"]


class PlatformPlan(Contract):
    platform: str
    target_url: str | None = None
    method: ReportMethod
    status: str = "prepared"  # prepared|confirmed
    evidence_attached: bool = False
    steps: list[str] = []
    prefilled_text: str = ""
    report_category: str | None = None
    card_url: str | None = None


class EvidenceBundle(Contract):
    verdict: dict
    sources: list[dict]
    forensic_report: dict | None = None
    media_hashes: list[dict] = []
    timestamps: dict[str, str] = {}
    audit_id: int | None = None
    pdf_url: str
    verify_url: str


class ReportDraftPackage(Contract):
    job_id: str
    eligible: bool
    ineligible_reason: str | None = None
    preview_text: str = ""  # exactly what will be sent
    bundle: EvidenceBundle | None = None
    bundle_url: str | None = None
    platform_plans: list[PlatformPlan] = []
    audit_id: int | None = None  # audit row for report_prepared


class ReportConfirmRequest(Contract):
    platform: str
    confirmed: bool  # must be true: the user clicked "I confirm"


class ReportConfirmResponse(Contract):
    attempt_logged: bool
    attempt_id: str
    platform: str
    method: ReportMethod
    reporter_agent_command: str | None = None  # command for the presenter-side Playwright helper
    audit_id: int | None = None


# ---- Legal ---------------------------------------------------------------------------------

Jurisdiction = Literal["IN", "EU", "US"]


class LegalAcknowledgements(Contract):
    is_subject_or_authorized: bool
    not_legal_advice: bool
    no_auto_send: bool
    will_review_before_filing: bool


class LegalDraftRequest(Contract):
    jurisdiction: Jurisdiction
    acknowledgements: LegalAcknowledgements
    platform: str | None = None


class LegalDraftResponse(Contract):
    draft_text: str
    jurisdiction: Jurisdiction
    checklist: list[str]
    filing_instructions: list[str] = []
    placeholders: list[str] = []
    template_version: str
    disclaimer: str
    evidence_bundle_ref: str
    audit_id: int | None = None


# ---- Audit ---------------------------------------------------------------------------------


class AuditEntryView(Contract):
    id: int
    job_id: str
    event_type: AuditEventType
    created_at: datetime
    prev_hash: str
    payload_hash: str
    payload_json: str  # canonical JSON text exactly as hashed
    resolved_model_ids: dict[str, str]
    tool_versions: dict[str, str]
    signature: str  # base64 Ed25519 over f"{prev_hash}:{payload_hash}"
    key_fingerprint: str


class AuditVerifyResult(Contract):
    ok: bool
    checked_rows: int
    broken_at: int | None = None
    reason: str | None = None
    head_hash: str | None = None
    job_entry_ids: list[int] = []


# ---- Health --------------------------------------------------------------------------------


class ToolStatusView(Contract):
    configured: bool
    state: ToolState
    version: str = ""
    calls: int = 0
    failures: int = 0
    last_error: str | None = None


class HealthView(Contract):
    status: Literal["ok", "degraded"]
    mode: str
    version: str
    resolved_models: dict[str, str]
    tool_status: dict[str, ToolStatusView]
    db: str
    ml: dict[str, str] = {}  # model -> loaded|loading|unavailable|disabled
    llm_provider: str
