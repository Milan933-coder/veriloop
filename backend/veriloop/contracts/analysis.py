"""Forensic and provenance branch outputs."""

from datetime import datetime

from .common import Contract, SamplingInfo, ToolError
from .enums import BranchStatus, ForensicStatus
from .evidence import EvidenceItem


class ModelScore(Contract):
    model_id: str  # exact HF repo id or commercial model name
    model_version: str = ""  # HF commit sha / API version
    provider: str  # local|hf_inference|sightengine
    raw_score: float | None = None  # probability the media is fake, 0..1
    weight: float = 0.0
    frames_scored: int = 0
    latency_ms: int = 0
    error: str | None = None


class AbstainBand(Contract):
    low: float
    high: float
    delta: float


class MetadataReport(Contract):
    exif_present: bool = False
    exif_software: str | None = None
    exif_stripped_suspected: bool = False
    reencode_hints: list[str] = []
    container: str | None = None
    c2pa_present: bool = False
    c2pa_signer: str | None = None
    c2pa_issuer: str | None = None
    c2pa_actions: list[str] = []
    c2pa_note: str = "Absence of C2PA Content Credentials is not evidence of manipulation."


class ForensicResult(Contract):
    branch_status: BranchStatus
    status: ForensicStatus
    ensemble_score: float | None = None  # weighted P(fake)
    disagreement: float | None = None  # max - min across model scores
    band: AbstainBand
    models: list[ModelScore] = []
    faces_found: int = 0
    frames_analyzed: int = 0
    sampling: SamplingInfo | None = None
    metadata: MetadataReport | None = None
    asr_ocr_mismatch: float | None = None
    synthetic_speech_score: float | None = None
    notes: list[str] = []
    errors: list[ToolError] = []
    tools_used: dict[str, str] = {}
    latency_ms: int = 0


class Appearance(Contract):
    url: str
    page_title: str | None = None
    first_seen_at: datetime | None = None
    timestamp_source: str = "unknown"  # article:published_time|http_last_modified|gdelt|known_fake_manifest|unknown
    match_type: str  # full|partial|page|phash|clip
    domain: str | None = None
    context_flags: list[str] = []  # satire|state_affiliated|known_disinfo


class KnownFakeMatch(Contract):
    known_fake_id: str
    label: str  # manipulated|out_of_context|authentic_reference
    hamming: int | None = None
    clip_similarity: float | None = None
    source_urls: list[str] = []  # fact-check URLs from the seed manifest
    title: str | None = None
    notes: str | None = None


class ProvenanceResult(Contract):
    branch_status: BranchStatus
    first_seen_estimate: Appearance | None = None
    appearances: list[Appearance] = []
    known_fake_matches: list[KnownFakeMatch] = []
    prior_run_matches: list[str] = []  # job ids with near-duplicate media
    out_of_context_score: float = 0.0  # 0..1; earliest appearance predates the claimed event
    context_flags: list[str] = []
    items: list[EvidenceItem] = []  # citable pages (ids P1..)
    notes: list[str] = []
    errors: list[ToolError] = []
    tools_used: dict[str, str] = {}
    latency_ms: int = 0
