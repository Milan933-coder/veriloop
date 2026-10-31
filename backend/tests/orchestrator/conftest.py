"""Actions test settings: every test signs with one session audit key (tests/orchestrator/builders.py).

`deps` runs on a database of this module's own, so whole-chain verify_chain checks are independent of
rows other packages' tests write into the shared session database.
"""

from collections.abc import AsyncIterator, Callable

import pytest

from tests.orchestrator.builders import SESSION_AUDIT_KEY, fresh_database
from veriloop.bootstrap import build_deps_for_tests
from veriloop.core.config import Settings
from veriloop.core.testing import FakeLLM
from veriloop.core.tool_client import ToolClient
from veriloop.db.engine import Database
from veriloop.deps import Deps


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(audit_ed25519_key=SESSION_AUDIT_KEY, app_base_url="http://testserver")


@pytest.fixture(scope="module")
async def actions_db(db: Database) -> AsyncIterator[Database]:
    async with fresh_database(db, "vl_orch") as fresh:
        yield fresh


@pytest.fixture
async def deps(settings: Settings, actions_db: Database, tool_client: ToolClient, fake_llm: FakeLLM) -> Deps:
    return build_deps_for_tests(
        settings,
        db=actions_db,
        tools=tool_client,
        llm=fake_llm,
        resolved_models={"llm_primary": fake_llm.model_id},
    )
