"""Actions test settings: every test signs with one session audit key (tests/orchestrator/builders.py)."""

from collections.abc import Callable

import pytest

from tests.orchestrator.builders import SESSION_AUDIT_KEY
from veriloop.core.config import Settings


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(audit_ed25519_key=SESSION_AUDIT_KEY, app_base_url="http://testserver")
