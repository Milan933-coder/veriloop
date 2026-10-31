"""Structured-output schemas for the debate LLM calls (internal; contracts are built from them).

Kept minimal for strict json_schema mode (D-01a): no dict fields, no URL fields (an agent can never
emit a URL, so it can never invent one), ids only as plain strings that the verifier re-checks.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Point(BaseModel):
    text: str = Field(description="one sentence restating what the cited evidence items say")
    evidence_ids: list[str] = Field(description="ids of the evidence items that state this, e.g. E3, P1, D2")


class AgentOut(BaseModel):
    points: list[Point] = Field(description="at most 4 points, each citing evidence ids")
    followup_query: str | None = Field(
        description="ONE short news search query (keywords) that could find more evidence for your "
        "side, or null if the evidence table is already sufficient"
    )


class JudgeOut(BaseModel):
    label: Literal["TRUE", "FALSE", "MANIPULATED", "UNVERIFIABLE"]
    rationale: list[Point] = Field(description="2-4 sentences, each citing the evidence ids it restates")


class SupportCheck(BaseModel):
    index: int = Field(description="the sentence index exactly as given")
    supported: bool = Field(description="true only if the cited TEXT alone states everything in the sentence")
    reason: str = Field(description="short reason; name the unsupported part when false")


class SupportBatch(BaseModel):
    checks: list[SupportCheck]
