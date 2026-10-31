"""The dependency container handed to every service (BUILD_SPEC §4). Built by `bootstrap.startup`."""

from __future__ import annotations

from dataclasses import dataclass

from veriloop.core.config import Settings
from veriloop.core.events import EventBus
from veriloop.core.llm import LLMClient
from veriloop.core.ml import NLI, Embedder
from veriloop.core.tool_client import ToolClient
from veriloop.db.engine import Database


@dataclass
class Deps:
    settings: Settings  # core.config.get_settings()
    db: Database  # db.engine.Database (asyncpg pool + pgvector codec)
    tools: ToolClient  # core.tool_client (chaos state: deps.tools.chaos)
    llm: LLMClient  # primary (may be DisabledLLM)
    llm2: LLMClient | None  # optional independent judge
    embedder: Embedder  # core.ml: multilingual e5-small, 384-d, lazy
    nli: NLI  # core.ml: multilingual NLI cross-encoder, lazy
    events: EventBus  # core.events: per-job pub/sub for SSE
    resolved_models: dict[str, str]
