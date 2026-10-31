"""LangGraph state. Parallel branches append to `trace` and `tool_errors` through reducers."""

import operator
from typing import Annotated, TypedDict

from .analysis import ForensicResult, ProvenanceResult
from .api import VerifyRequest
from .common import NodeTiming, ToolError
from .evidence import BranchResult
from .media import ClaimExtraction, ResolvedInput
from .reasoning import DebateResult, FusionResult, Verdict


class JobState(TypedDict, total=False):
    job_id: str
    request: VerifyRequest
    upload_path: str | None
    resolved: ResolvedInput
    extraction: ClaimExtraction
    evidence: BranchResult
    forensic: ForensicResult
    provenance: ProvenanceResult
    fusion: FusionResult
    debate: DebateResult
    verdict: Verdict
    audit_id: int
    trace: Annotated[list[NodeTiming], operator.add]
    tool_errors: Annotated[list[ToolError], operator.add]
