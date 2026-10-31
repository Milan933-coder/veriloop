"""Live aicredits smoke tests (`-m network`): real resolution + probe, one real complete_json.

Uses the real repo-root `.env` (LLM_API_KEY). Costs about INR 0.002 per run.
"""

import time

import pytest
from pydantic import BaseModel

from veriloop.bootstrap import build_llm_clients
from veriloop.contracts import JudgeOutput, VerdictLabel
from veriloop.core.config import Settings
from veriloop.core.logging import register_secrets
from veriloop.core.tool_client import ToolClient

pytestmark = pytest.mark.network


class Capital(BaseModel):
    country: str
    capital: str
    confidence: float
    note: str | None = None


@pytest.fixture
async def live(tmp_path):
    s = Settings(cassette_dir=tmp_path / "cassettes", tool_mode="live")
    if not s.llm_enabled:
        pytest.skip("no LLM_API_KEY in .env")
    register_secrets(s.secret_values())
    tools = ToolClient(s)
    yield s, tools
    await tools.aclose()


async def test_live_model_resolution_and_complete_json(live) -> None:
    s, tools = live
    t0 = time.perf_counter()
    llm, llm2, resolved = await build_llm_clients(s, tools, None)
    resolve_ms = (time.perf_counter() - t0) * 1000
    print(f"\nRESOLVED {resolved} in {resolve_ms:.0f} ms (list + probe)")
    assert resolved["llm_primary"].rsplit("/", 1)[-1] == "gpt-6-luna"
    assert llm2 is not None and resolved["llm_secondary"].rsplit("/", 1)[-1] == "gpt-5.6-luna"

    t1 = time.perf_counter()
    out = await llm.complete_json(
        task="smoke_capital",
        system="You answer geography questions as JSON.",
        user="What is the capital of India? Leave note null.",
        schema=Capital,
        effort="none",
        max_tokens=200,
    )
    print(f"PRIMARY complete_json -> {out.model_dump()} in {(time.perf_counter() - t1) * 1000:.0f} ms")
    assert "delhi" in out.capital.lower()

    t2 = time.perf_counter()
    out2 = await llm2.complete_json(
        task="smoke_capital",
        system="You answer geography questions as JSON.",
        user="What is the capital of France? Leave note null.",
        schema=Capital,
    )
    print(f"SECONDARY complete_json -> {out2.model_dump()} in {(time.perf_counter() - t2) * 1000:.0f} ms")
    assert "paris" in out2.capital.lower()

    # A real nested contract (enum + $defs + list of objects) proves the strict transform is accepted
    # upstream; low effort as the judge would use.
    t3 = time.perf_counter()
    judge = await llm.complete_json(
        task="smoke_judge",
        system="You are a fact-check judge. Use only the evidence given; cite evidence ids.",
        user=(
            "Claim: The Eiffel Tower is in Paris.\n"
            "Evidence E1 (britannica.com): The Eiffel Tower is a wrought-iron tower in Paris, France.\n"
            "Evidence E2 (wikipedia.org): The Eiffel Tower stands on the Champ de Mars in Paris.\n"
            "Return label, a one-sentence rationale citing ids, provider 'aicredits', model_id 'judge', "
            "extractive false."
        ),
        schema=JudgeOutput,
        effort="low",
        max_tokens=400,
    )
    print(
        f"PRIMARY JudgeOutput -> {judge.model_dump(mode='json')} "
        f"in {(time.perf_counter() - t3) * 1000:.0f} ms"
    )
    assert judge.label is VerdictLabel.TRUE and judge.rationale
