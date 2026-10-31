"""Fixed enumerations shared by every service. Values are wire-format strings."""

from enum import Enum


class VerdictLabel(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    MANIPULATED = "MANIPULATED"  # reserved for synthetic / deepfake findings
    UNVERIFIABLE = "UNVERIFIABLE"  # the abstain state
    NOT_CHECKABLE = "NOT_CHECKABLE"  # cheap-gate output


class AbstainReason(str, Enum):
    NO_EVIDENCE = "NO_EVIDENCE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    SINGLE_SOURCE = "SINGLE_SOURCE"
    CONFLICT = "CONFLICT"
    FORENSIC_INCONCLUSIVE = "FORENSIC_INCONCLUSIVE"
    UNGROUNDED_RATIONALE = "UNGROUNDED_RATIONALE"
    JUDGE_DISAGREEMENT = "JUDGE_DISAGREEMENT"
    TOOL_OUTAGE = "TOOL_OUTAGE"
    LANGUAGE_UNSUPPORTED = "LANGUAGE_UNSUPPORTED"


class EvidenceState(str, Enum):
    """What the evidence says, independent of the final label. Drives G8 in the UI."""

    SUPPORTS = "supports"
    REFUTES = "refutes"
    MANIPULATED = "manipulated"
    CONFLICTING = "conflicting"
    INSUFFICIENT = "insufficient"  # some evidence, not enough to decide
    NO_EVIDENCE = "no_evidence"  # nothing relevant was found (NOT the same as refutes)
    NOT_CHECKABLE = "not_checkable"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class BranchStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"  # some tools failed, partial results
    INSUFFICIENT = "insufficient"  # tools failed or returned nothing usable
    FAILED = "failed"  # branch crashed (caught)
    SKIPPED = "skipped"  # not applicable (e.g. forensics on text-only input)


class Stance(str, Enum):
    SUPPORT = "support"
    REFUTE = "refute"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class ForensicStatus(str, Enum):
    REAL = "real"
    MANIPULATED = "manipulated"
    INCONCLUSIVE = "inconclusive"
    UNAVAILABLE = "unavailable"


class Platform(str, Enum):
    X = "x"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    WEB = "web"  # any other URL
    UPLOAD = "upload"
    TEXT = "text"


class InputKind(str, Enum):
    URL = "url"
    UPLOAD = "upload"
    TEXT = "text"


class MediaKind(str, Enum):
    IMAGE = "image"
    VIDEO_FRAME = "video_frame"
    THUMBNAIL = "thumbnail"


class RetrievalMethod(str, Enum):
    FACTCHECK_CLAIMSEARCH = "factcheck_claimsearch"
    FACTCHECK_IMAGESEARCH = "factcheck_imagesearch"
    GOOGLE_NEWS_RSS = "google_news_rss"
    WIRE_RSS = "wire_rss"
    PIB_RSS = "pib_rss"
    GDELT_DOC = "gdelt_doc"
    SERPER_WEB = "serper_web"
    SERPER_NEWS = "serper_news"
    WIKIDATA_SPARQL = "wikidata_sparql"
    VISION_WEB_DETECTION = "vision_web_detection"
    PHASH_INDEX = "phash_index"
    CLIP_INDEX = "clip_index"
    FOLLOWUP_SEARCH = "followup_search"


class AuditEventType(str, Enum):
    VERDICT = "verdict"
    REPORT_PREPARED = "report_prepared"
    REPORT_CONFIRMED = "report_confirmed"
    LEGAL_DRAFT_GENERATED = "legal_draft_generated"


class ToolState(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    OPEN = "open"  # circuit breaker open
    UNCONFIGURED = "unconfigured"  # missing key
    DISABLED = "disabled"
    CHAOS = "chaos"  # fault injected via /admin/chaos
