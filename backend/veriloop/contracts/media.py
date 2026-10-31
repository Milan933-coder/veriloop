from datetime import datetime

from .common import Contract, SamplingInfo
from .enums import InputKind, MediaKind, Platform


class MediaAsset(Contract):
    id: str
    kind: MediaKind
    path: str  # local filesystem path under VERILOOP_HOME/media/{job_id}; never exposed via API
    sha256: str
    phash: str | None = None  # 16 hex chars (64-bit perceptual hash)
    frame_ts: float | None = None  # seconds into the video
    width: int | None = None
    height: int | None = None
    source: str = "download"  # download|upload|page_capture|thumbnail


class ResolvedInput(Contract):
    input_kind: InputKind
    platform: Platform
    original_url: str | None = None
    normalized_url: str | None = None
    title: str | None = None
    author: str | None = None
    caption_text: str | None = None  # caption / post text / user-supplied text
    published_at: datetime | None = None
    oembed: dict | None = None
    media: list[MediaAsset] = []  # images or sampled frames
    video_path: str | None = None
    audio_path: str | None = None
    duration_s: float | None = None
    sampling: SamplingInfo | None = None
    media_accessible: bool = False
    fetch_methods: list[str] = []  # e.g. ["oembed", "yt-dlp"] or ["page_meta", "playwright"]
    labels: list[str] = []  # user-visible caveats, e.g. "Visual forensics unavailable - caption only"


class Entity(Contract):
    name: str
    type: str = "other"  # person|org|place|event|other
    wikidata_id: str | None = None


class Claim(Contract):
    id: str  # C1, C2 ...
    text: str  # in the original language
    text_en: str | None = None  # English rendering for search (E9)
    entities: list[Entity] = []
    claim_type: str = "event"  # event|statistic|quote|identity|other
    time_reference: str | None = None
    location: str | None = None


class SubjectInfo(Contract):
    is_identifiable_individual: bool = False
    is_private_individual: bool | None = None  # None = unknown -> treat as private (G9 caution)
    name: str | None = None


class ClaimExtraction(Contract):
    caption_text: str | None = None
    transcript: str | None = None
    transcript_language: str | None = None
    ocr_text: str | None = None
    language: str = "en"  # ISO 639-1 of the primary claim
    claims: list[Claim] = []
    primary_claim_id: str | None = None
    check_worthiness: float = 0.0  # 0..1
    worthiness_reason: str = ""
    subject: SubjectInfo = SubjectInfo()
    asr_ocr_mismatch: float | None = None  # 0..1, higher = spoken words disagree with on-screen text
    llm_used: bool = False
    notes: list[str] = []

    def primary_claim(self) -> Claim | None:
        for c in self.claims:
            if c.id == self.primary_claim_id:
                return c
        return self.claims[0] if self.claims else None
