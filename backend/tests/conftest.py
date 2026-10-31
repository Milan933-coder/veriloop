"""Shared pytest fixtures for every VeriLoop package. No network, no model downloads.

Fixtures (use these names; the event loop is session-scoped per pyproject):

* `settings` - a `Settings` that ignores the real `.env`: VERILOOP_HOME and cassette dir under a
  per-test tmp dir, TOOL_MODE=live, LLM_PROVIDER=none (no key), ML_ENABLE=false, no tool keys.
* `make_settings(**overrides)` - factory for a variant, e.g. `make_settings(serper_api_key="k",
  tool_mode="replay")` or `make_settings(llm_provider="aicredits", llm_api_key="test-key")`.
* `db` (session) - a `Database` on a fresh, uniquely named database (schema applied) on the shared
  embedded pgserver (VERILOOP_HOME/pgdata) or on `VERILOOP_TEST_DATABASE_URL` (admin DSN) if set.
  Dropped at session end. Shared across tests: always use unique ids (`new_job_id()`).
* `fake_clock` / `fake_sleep` - `FakeClock` + `RecordingSleep` (sleep returns immediately, records
  delays, advances the clock), so retry/breaker tests never really wait.
* `tool_client` - `ToolClient(settings)` with an in-memory cache, fresh ChaosState, fake clock and
  sleep. HTTP goes through a real httpx.AsyncClient: mock it with `respx`.
* `fake_llm` - `FakeLLM` (enabled=True, provider="fake", model_id="fake-model"); script responses per
  task: `fake_llm.script("extract_claims", {...})`; inspect `fake_llm.calls`.
* `deps` - a full `Deps`: settings + db + tool_client + fake_llm (llm2=None), Embedder/NLI built
  with ML_ENABLE=false (`.available` is False; calls raise MLUnavailable), a fresh EventBus,
  resolved_models={"llm_primary": "fake-model"}. Swap parts per test, e.g.
  `deps.embedder = FakeEmbedder()` / `deps.nli = FakeNLI(fn)` / `deps.llm2 = FakeLLM(model_id="fake-2")`.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from veriloop.bootstrap import build_deps_for_tests
from veriloop.core.cache import ToolCache
from veriloop.core.chaos import ChaosState
from veriloop.core.config import ENV_FILE as REPO_ENV_FILE
from veriloop.core.config import Settings, default_home, get_settings
from veriloop.core.logging import clear_registered_secrets
from veriloop.core.testing import FakeClock, FakeLLM, RecordingSleep
from veriloop.core.tool_client import ToolClient
from veriloop.db.engine import Database, create_database, drop_database, embedded_server_dsn, with_database
from veriloop.deps import Deps

# Settings fields that read secrets/behaviour from the real environment; pinned for isolation.
_ISOLATED: dict[str, Any] = {
    "database_url": "",
    "llm_provider": "none",
    "llm_api_key": "",
    "google_factcheck_api_key": "",
    "google_vision_api_key": "",
    "serper_api_key": "",
    "sightengine_api_user": "",
    "sightengine_api_secret": "",
    "hf_token": "",
    "telegram_bot_token": "",
    "api_keys": "",
    "tool_mode": "live",
    "ml_enable": False,
    "ml_prewarm": False,
    "log_json": True,
}


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            **_ISOLATED,
            "veriloop_home": tmp_path / "home",
            "cassette_dir": tmp_path / "cassettes",
            **overrides,
        }
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    return factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture(autouse=True)
def _reset_redaction() -> Iterator[None]:
    yield
    clear_registered_secrets()


@pytest.fixture(scope="session")
async def db() -> AsyncIterator[Database]:
    # Share the dev server's data dir (the real .env's VERILOOP_HOME, e.g. on D:) rather than the
    # code default under the C: profile; only the test *settings* ignore .env, not the server location.
    admin_dsn = os.environ.get("VERILOOP_TEST_DATABASE_URL") or await embedded_server_dsn(
        get_settings().pgdata_dir if REPO_ENV_FILE.exists() else default_home() / "pgdata"
    )
    name = f"vl_test_{os.getpid()}_{secrets.token_hex(3)}"
    await create_database(admin_dsn, name)
    database = await Database.connect(dsn=with_database(admin_dsn, name), min_size=1, max_size=5)
    try:
        await database.apply_schema()
        yield database
    finally:
        await database.close()
        await drop_database(admin_dsn, name)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_sleep(fake_clock: FakeClock) -> RecordingSleep:
    return RecordingSleep(fake_clock)


@pytest.fixture
async def tool_client(
    settings: Settings, fake_clock: FakeClock, fake_sleep: RecordingSleep
) -> AsyncIterator[ToolClient]:
    client = ToolClient(
        settings,
        cache=ToolCache(clock=fake_clock),
        chaos=ChaosState(),
        clock=fake_clock,
        sleep=fake_sleep,
        rng=lambda: 0.0,  # no jitter: deterministic backoff delays
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def deps(settings: Settings, db: Database, tool_client: ToolClient, fake_llm: FakeLLM) -> Deps:
    return build_deps_for_tests(
        settings, db=db, tools=tool_client, llm=fake_llm, resolved_models={"llm_primary": fake_llm.model_id}
    )
