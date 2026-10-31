from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Contract(BaseModel):
    """Base for every cross-service model. Unknown fields are rejected so drift fails loudly."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ToolError(Contract):
    """A logged tool failure (G3). Never replaced by fabricated data."""

    tool: str
    op: str = ""
    error_type: str  # timeout|http_error|circuit_open|unconfigured|replay_miss|parse_error|chaos|refusal|rate_limited|exception
    message: str
    attempts: int = 1
    at: datetime = Field(default_factory=utcnow)


class NodeTiming(Contract):
    node: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    status: str = "ok"  # ok|error|skipped
    detail: str | None = None


class SamplingInfo(Contract):
    fps: float
    frames: int
    duration_s: float | None = None
    capped: bool = False
    note: str | None = None  # e.g. "Sampled 16 of 412 s at 1 fps (cap reached)"
