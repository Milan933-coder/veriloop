"""LangGraph StateGraph over `JobState`, exactly as BUILD_SPEC §6:

    START -> resolve_input -> extract_claim -> check_worthiness
    check_worthiness --(worthiness < threshold or no claims)--> decide_action
    check_worthiness --> evidence_branch, forensic_branch, provenance_branch      (parallel fan-out)
    [evidence_branch, forensic_branch, provenance_branch] -> fuse_evidence      (join)
    fuse_evidence --(candidate UNVERIFIABLE)--> decide_action                    (skip debate)
    fuse_evidence --> judge -> decide_action -> build_report -> audit_write -> END

The three branches run in the same superstep, i.e. concurrently on the event loop. A node error also
routes to decide_action (which emits UNVERIFIABLE/TOOL_OUTAGE) so the run is always audited.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph

from veriloop.contracts import JobState, JobStatus, utcnow
from veriloop.deps import Deps

from .jobview import build_job_view
from .nodes import BRANCHES, PipelineNodes, ViewBuilder, traced
from .services import ServiceSet, default_services


def default_view_builder(deps: Deps) -> ViewBuilder:
    def build(state: Mapping[str, Any]) -> Any:
        request = state.get("request")
        return build_job_view(
            state,
            job_id=state["job_id"],
            status=JobStatus.COMPLETED,
            created_at=utcnow(),
            settings=deps.settings,
            resolved_models=deps.resolved_models,
            source=request.source if request else "web",
        )

    return build


def build_graph(
    deps: Deps, services: ServiceSet | None = None, view_builder: ViewBuilder | None = None
) -> Any:
    """Compile the pipeline graph (reusable across concurrent jobs: nodes are stateless closures)."""
    nodes = PipelineNodes(deps, services or default_services(), view_builder or default_view_builder(deps))
    graph = StateGraph(JobState)
    for name, fn in nodes.functions().items():
        graph.add_node(name, traced(name, fn, deps))
    graph.add_edge(START, "resolve_input")
    graph.add_edge("resolve_input", "extract_claim")
    graph.add_edge("extract_claim", "check_worthiness")
    graph.add_conditional_edges(
        "check_worthiness", nodes.route_after_worthiness, ["decide_action", *BRANCHES]
    )
    graph.add_edge(list(BRANCHES), "fuse_evidence")
    graph.add_conditional_edges("fuse_evidence", nodes.route_after_fusion, ["decide_action", "judge"])
    graph.add_edge("judge", "decide_action")
    graph.add_edge("decide_action", "build_report")
    graph.add_edge("build_report", "audit_write")
    graph.add_edge("audit_write", END)
    return graph.compile()
