"""Debate service (BUILD_SPEC §5; SPEC §5 Tool-MAD; D-13 dual-key verifier; D-16 LLM is never a source).

run_debate(extraction, fusion, items, forensic, provenance, deps) -> DebateResult
prewarm(deps) -> None
"""

from veriloop.deps import Deps
from veriloop.services.debate.service import extractive_judge, run_debate
from veriloop.services.debate.verifier import verify


async def prewarm(deps: Deps) -> None:
    """Load the local NLI model (the verifier's second key) before the first job."""
    if deps.nli.available:
        await deps.nli.load()


__all__ = ["extractive_judge", "prewarm", "run_debate", "verify"]
