"""Debate service (BUILD_SPEC §5; SPEC §5 Tool-MAD; D-13 dual-key verifier; D-16 LLM is never a source).

run_debate(extraction, fusion, items, forensic, provenance, deps) -> DebateResult
prewarm(deps) -> None
"""

import structlog

from veriloop.deps import Deps
from veriloop.services.debate.service import extractive_judge, run_debate
from veriloop.services.debate.verifier import verify

log = structlog.get_logger(__name__)


async def prewarm(deps: Deps) -> None:
    """Load and warm the local NLI model (the verifier's second key) before the first job.

    The first inference after loading is much slower than the rest (allocation, kernel setup), so one
    tiny pair is scored here instead of inside a job's verifier time budget.
    """
    if deps.nli.available and await deps.nli.load():
        try:
            await deps.nli.score([("The cabinet approved the hike.", "The hike was approved.")])
        except Exception as exc:  # warm-up only; the verifier handles an unavailable NLI itself
            log.warning("nli_warmup_failed", error=str(exc)[:200])


__all__ = ["extractive_judge", "prewarm", "run_debate", "verify"]
