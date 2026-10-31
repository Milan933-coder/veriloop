"""Evidence service (BUILD_SPEC §5): ClaimReview fast path + keyless deep path, stance, independence groups.

gather_evidence(extraction, resolved, locale, deps, *, job_id=None) -> BranchResult
followup_search(query, deps, *, start_index=1) -> list[EvidenceItem]   # ids D{n}
prewarm(deps) -> None
"""

from veriloop.deps import Deps
from veriloop.services.evidence.pipeline import followup_search, gather_evidence


async def prewarm(deps: Deps) -> None:
    """Load the embedder and NLI before the first job.

    NLI is the stance fallback for LLM batches that fail or time out (and the whole stance step when
    the LLM is disabled); loading ~560 MB lazily mid-job stalls the shared CPU pool for every job.
    """
    await deps.embedder.load()
    if deps.nli.available:
        await deps.nli.load()


__all__ = ["followup_search", "gather_evidence", "prewarm"]
