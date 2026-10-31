"""Fusion, debate, verifier and verdict models."""

from .common import Contract
from .enums import AbstainReason, EvidenceState, VerdictLabel
from .evidence import EvidenceItem


class SignalScore(Contract):
    name: str  # claimreview|news_stance|gdelt|wikidata|provenance|forensic
    score: float  # -1 (refutes) .. +1 (supports); forensic uses P(fake) on the manipulation axis
    weight: float
    reliability: float = 1.0
    present: bool = True  # absent signals are excluded from the average, never counted as negative (E4)
    detail: str = ""
    evidence_ids: list[str] = []


class FusionResult(Contract):
    truth_score: float  # T in [-1, 1]
    manipulation_score: float  # M in [0, 1]
    p_true: float  # calibrated P(claim true)
    signals: list[SignalScore] = []
    groups_support: int = 0  # independent publisher groups supporting the claim
    groups_refute: int = 0
    groups_manipulation: int = 0  # independent groups documenting manipulation
    candidate: VerdictLabel
    candidate_reason: AbstainReason | None = None
    evidence_state: EvidenceState
    weights_version: str = ""
    calibration_version: str = ""
    thresholds: dict[str, float] = {}
    fast_path: bool = False
    notes: list[str] = []


class RationaleSentence(Contract):
    text: str
    evidence_ids: list[str] = []


class Argument(Contract):
    role: str  # proponent|opponent
    points: list[RationaleSentence] = []
    followup_queries: list[str] = []
    followup_evidence_ids: list[str] = []


class JudgeOutput(Contract):
    label: VerdictLabel
    rationale: list[RationaleSentence] = []
    provider: str
    model_id: str
    extractive: bool = False  # True when LLM_PROVIDER=none and the rationale is quoted evidence


class GroundingCheck(Contract):
    sentence_index: int
    text: str
    evidence_ids: list[str] = []
    deterministic_ok: bool  # ids present, ids exist in this job, urls belong to this job
    llm_supported: bool | None = None
    nli_entailment: float | None = None  # local NLI cross-check (independent of the LLM)
    ok: bool
    reason: str = ""


class VerifierOutput(Contract):
    ok: bool
    checks: list[GroundingCheck] = []
    failing_sentences: list[str] = []
    methods: list[str] = []  # deterministic|llm|nli


class SecondJudge(Contract):
    provider: str
    model_id: str
    label: VerdictLabel | None = None
    agrees: bool | None = None
    error: str | None = None


class DebateResult(Contract):
    skipped: bool = False
    skip_reason: str | None = None
    proponent: Argument | None = None
    opponent: Argument | None = None
    judge: JudgeOutput | None = None
    verifier: VerifierOutput | None = None
    second_judge: SecondJudge | None = None
    followup_items: list[EvidenceItem] = []  # ids D1.. added during debate
    latency_ms: int = 0


class Verdict(Contract):
    label: VerdictLabel
    confidence: float  # calibrated, 0..1 (confidence in the emitted label)
    abstain_reason: AbstainReason | None = None
    abstain_detail: str | None = None
    evidence_state: EvidenceState
    rationale: list[RationaleSentence] = []
    cited_evidence_ids: list[str] = []
    independent_source_count: int = 0
    report_eligible: bool = False
    legal_eligible: bool = False
    legal_block_reason: str | None = None
    degraded_tools: list[str] = []
    labels: list[str] = []  # user-visible caveats carried from every stage
