"""API test harness: the real app (create_app) + lifespan + httpx AsyncClient over ASGI, fake services.

Every test signs with one session audit key (tests/orchestrator/builders.py), like the other actions
test packages, because verify_chain checks the whole shared chain.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from tests.orchestrator.builders import SESSION_AUDIT_KEY, FakeServices, fresh_database
from veriloop.api import create_app
from veriloop.api.runtime import AppRuntime
from veriloop.bootstrap import build_deps_for_tests
from veriloop.contracts import JobView
from veriloop.core.config import Settings
from veriloop.core.testing import FakeLLM
from veriloop.core.tool_client import ToolClient
from veriloop.db.engine import Database
from veriloop.deps import Deps

BASE_URL = "http://testserver"


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings(audit_ed25519_key=SESSION_AUDIT_KEY, app_base_url=BASE_URL)


@pytest.fixture(scope="module")
async def actions_db(db: Database) -> AsyncIterator[Database]:
    """Own database: whole-chain verify_chain must not see other packages' rows in the shared one."""
    async with fresh_database(db, "vl_api") as fresh:
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


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    fakes: FakeServices

    @property
    def runtime(self) -> AppRuntime:
        return self.app.state.runtime

    async def submit(self, body: dict[str, Any] | None = None, **kw: Any) -> str:
        resp = await self.client.post(
            "/verify", json=body or {"text": "Viral claim", "locale": "en-IN"}, **kw
        )
        assert resp.status_code == 202, resp.text
        return resp.json()["job_id"]

    async def finish(self, job_id: str) -> JobView:
        view = await self.runtime.runner.wait(job_id, timeout=30)
        assert view is not None
        return view

    async def completed_job(self, body: dict[str, Any] | None = None) -> str:
        job_id = await self.submit(body)
        await self.finish(job_id)
        return job_id


HarnessFactory = Callable[..., Any]


@pytest.fixture
def api(deps: Deps, make_settings: Callable[..., Settings]) -> HarnessFactory:
    """`async with api(fakes=..., dist=..., **settings_overrides) as h:` -> Harness."""

    @asynccontextmanager
    async def factory(
        *, fakes: FakeServices | None = None, dist: Path | None = None, **overrides: Any
    ) -> AsyncIterator[Harness]:
        if overrides:
            deps.settings = make_settings(
                audit_ed25519_key=SESSION_AUDIT_KEY, app_base_url=BASE_URL, **overrides
            )
        fakes = fakes or FakeServices()
        app = create_app(deps=deps, services=fakes.service_set(), frontend_dist=dist)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url=BASE_URL, timeout=30) as client:
                yield Harness(app=app, client=client, fakes=fakes)

    return factory


def assert_meta(resp: httpx.Response) -> dict[str, Any]:
    """Every JSON body carries request_id + latency_ms, mirrored in the headers."""
    body = resp.json()
    assert isinstance(body["request_id"], str) and body["request_id"]
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert resp.headers["x-request-id"] == body["request_id"]
    assert int(resp.headers["x-latency-ms"]) >= 0
    return body


def assert_problem(resp: httpx.Response, status: int) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = assert_meta(resp)
    for key in ("type", "title", "status", "detail", "instance", "request_id"):
        assert key in body, key
    assert body["status"] == status and body["title"]
    return body


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Unnamed `data:` messages -> JSON payloads (plus the SSE id, when present, as `_id`)."""
    import json

    events: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")]
        ids = [line[3:].strip() for line in block.split("\n") if line.startswith("id:")]
        names = [line[6:].strip() for line in block.split("\n") if line.startswith("event:")]
        if not data:
            continue
        assert not names, "events must be unnamed data: messages"
        event = json.loads("\n".join(data))
        if ids:
            event["_id"] = ids[0]
        events.append(event)
    return events
