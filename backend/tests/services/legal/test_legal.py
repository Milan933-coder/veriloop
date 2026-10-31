"""Legal: eligibility matrix (incl. G9), acknowledgement gate, templates, LLM fact validation, storage."""

from __future__ import annotations

import json

import pytest

from tests.orchestrator.builders import make_view
from veriloop.contracts import (
    AuditEventType,
    ForensicStatus,
    JobStatus,
    JobView,
    LegalAcknowledgements,
    LegalDraftRequest,
    VerdictLabel,
    VerifyRequest,
)
from veriloop.core.errors import ProblemError
from veriloop.core.hashing import sha256_hex
from veriloop.core.ids import new_job_id
from veriloop.core.llm import DisabledLLM
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.services.audit import get_entry
from veriloop.services.legal import DISCLAIMER, generate_legal_draft, legal_eligibility, load_template

ACKS = LegalAcknowledgements(
    is_subject_or_authorized=True, not_legal_advice=True, no_auto_send=True, will_review_before_filing=True
)


def _view(**kw: object) -> JobView:
    defaults: dict[str, object] = {
        "identifiable": True,
        "private": False,
        "audit_id": 11,
        "forensic_status": ForensicStatus.MANIPULATED,
        "groups_manipulation": 2,
    }
    defaults.update(kw)
    label = defaults.pop("label", VerdictLabel.MANIPULATED)
    return make_view(label, job_id=defaults.pop("job_id", new_job_id()), **defaults)  # type: ignore[arg-type]


async def _job(deps: Deps, **kw: object) -> JobView:
    view = _view(**kw)
    await repo.create_job(deps.db, view.job_id, request=VerifyRequest(text="x"), status=JobStatus.COMPLETED)
    return view


@pytest.mark.parametrize(
    ("label", "identifiable", "private", "forensic", "groups", "eligible", "reason_part"),
    [
        (VerdictLabel.MANIPULATED, True, False, ForensicStatus.INCONCLUSIVE, 0, True, None),  # public figure
        (VerdictLabel.MANIPULATED, True, True, ForensicStatus.MANIPULATED, 2, True, None),
        (VerdictLabel.MANIPULATED, True, True, ForensicStatus.MANIPULATED, 1, False, "independent sources"),
        (VerdictLabel.MANIPULATED, True, True, ForensicStatus.INCONCLUSIVE, 3, False, "forensic analysis"),
        (VerdictLabel.MANIPULATED, True, None, ForensicStatus.REAL, 0, False, "G9"),  # unknown -> private
        (VerdictLabel.MANIPULATED, False, None, ForensicStatus.MANIPULATED, 3, False, "identifiable"),
        (VerdictLabel.FALSE, True, False, ForensicStatus.MANIPULATED, 3, False, "MANIPULATED"),
        (VerdictLabel.TRUE, True, False, ForensicStatus.REAL, 0, False, "MANIPULATED"),
        (VerdictLabel.UNVERIFIABLE, True, True, ForensicStatus.MANIPULATED, 2, False, "MANIPULATED"),
    ],
)
def test_eligibility_matrix(label, identifiable, private, forensic, groups, eligible, reason_part) -> None:  # type: ignore[no-untyped-def]
    job = _view(
        label=label,
        identifiable=identifiable,
        private=private,
        forensic_status=forensic,
        groups_manipulation=groups,
    )
    ok, reason = legal_eligibility(job)
    assert ok is eligible
    if reason_part:
        assert reason and reason_part in reason
    else:
        assert reason is None


def test_unfinished_job_not_eligible() -> None:
    ok, reason = legal_eligibility(_view(status=JobStatus.PROCESSING))
    assert not ok and reason and "finished" in reason


@pytest.mark.parametrize(
    "missing", ["is_subject_or_authorized", "not_legal_advice", "no_auto_send", "will_review_before_filing"]
)
async def test_every_acknowledgement_is_required(deps: Deps, missing: str) -> None:
    job = await _job(deps)
    acks = ACKS.model_copy(update={missing: False})
    with pytest.raises(ProblemError) as exc:
        await generate_legal_draft(job, LegalDraftRequest(jurisdiction="IN", acknowledgements=acks), deps)
    assert exc.value.status == 422 and exc.value.extra["missing_acknowledgements"] == [missing]
    assert await repo.list_legal_drafts(deps.db, job.job_id) == []


async def test_ineligible_job_gets_409(deps: Deps) -> None:
    job = await _job(deps, label=VerdictLabel.FALSE)
    with pytest.raises(ProblemError) as exc:
        await generate_legal_draft(job, LegalDraftRequest(jurisdiction="EU", acknowledgements=ACKS), deps)
    assert exc.value.status == 409


@pytest.mark.parametrize(
    ("jurisdiction", "must_contain"),
    [
        (
            "IN",
            [
                "Rule 3(2)(b)",
                "24 hours",
                "artificially morphed",
                "Digital Personal Data Protection Act, 2023",
                "phases",
                "https://cybercrime.gov.in",
                "Grievance Officer",
            ],
        ),
        ("EU", ["Article 17", "Art. 17(1)(d)", "Art. 12(3)", "Art. 77", "one month"]),
        (
            "US",
            [
                "512(c)",
                "ONLY if you own the copyright",
                "TAKE IT DOWN",
                "48 hours",
                "STATE LAW",
                "under penalty of perjury",
            ],
        ),
    ],
)
async def test_template_only_draft_when_llm_disabled(
    deps: Deps, jurisdiction: str, must_contain: list[str]
) -> None:
    deps.llm = DisabledLLM()
    job = await _job(deps)
    resp = await generate_legal_draft(
        job,
        LegalDraftRequest(jurisdiction=jurisdiction, acknowledgements=ACKS, platform="x"),
        deps,  # type: ignore[arg-type]
    )
    text = resp.draft_text
    assert text.count(DISCLAIMER) >= 2 and resp.disclaimer == DISCLAIMER
    for needle in must_contain:
        assert needle in text, needle
    assert (
        "[FULL NAME]" in resp.placeholders and "[DESCRIBE THE CONTENT IN ONE SENTENCE]" in resp.placeholders
    )
    assert all(p in text for p in resp.placeholders)
    assert "Jane Doe" not in text  # victim details are always placeholders
    assert job.input.url in text and job.sources[0].url in text
    assert f"/audit/{job.job_id}/pdf" in text and "ab" * 32 in text  # bundle reference: PDF link + media hash
    assert f"audit entry #{job.audit_id}" in resp.evidence_bundle_ref
    assert resp.template_version == load_template(jurisdiction).version and resp.checklist
    assert resp.filing_instructions and resp.jurisdiction == jurisdiction

    rows = await repo.list_legal_drafts(deps.db, job.job_id)
    assert len(rows) == 1 and rows[0].draft_sha256 == sha256_hex(text) and rows[0].audit_id == resp.audit_id
    columns = {
        r["column_name"]
        for r in await deps.db.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'legal_drafts'"
        )
    }
    assert not columns & {"draft_text", "text", "body", "content"}  # the draft itself is never stored
    entry = await get_entry(deps, job.job_id, AuditEventType.LEGAL_DRAFT_GENERATED)
    assert entry is not None and entry.id == resp.audit_id
    payload = json.loads(entry.payload_json)
    assert payload["draft_sha256"] == sha256_hex(text) and payload["sent"] is False
    assert payload["llm_filled_facts"] is False and text not in entry.payload_json


async def test_llm_fills_only_grounded_facts(deps: Deps, fake_llm) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script(
        "legal_facts",
        {
            "content_description": "A video in which the person depicted appears to call a country "
            "a failed state.",
            "manipulation_summary": [
                {
                    "text": "Two fact-checkers report that the video was digitally altered.",
                    "evidence_ids": ["E1", "E2"],
                },
                {"text": "It was first posted in 2019.", "evidence_ids": ["E9"]},  # unknown id -> dropped
                {
                    "text": "See https://evil.example/fake for details.",
                    "evidence_ids": ["E1"],
                },  # URL -> dropped
            ],
        },
    )
    job = await _job(deps)
    resp = await generate_legal_draft(job, LegalDraftRequest(jurisdiction="IN", acknowledgements=ACKS), deps)
    assert "digitally altered. (sources E1, E2)" in resp.draft_text
    assert "2019" not in resp.draft_text and "evil.example" not in resp.draft_text
    assert "appears to call a country a failed state" in resp.draft_text
    call = fake_llm.calls_for("legal_facts")[0]
    assert call.effort == "none" and "E1" in str(call.user)
    entry = await get_entry(deps, job.job_id, AuditEventType.LEGAL_DRAFT_GENERATED)
    assert entry is not None and json.loads(entry.payload_json)["llm_filled_facts"] is True


async def test_ungrounded_llm_output_falls_back_to_extractive(deps: Deps, fake_llm) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script(
        "legal_facts",
        {"content_description": "x", "manipulation_summary": [{"text": "Made up.", "evidence_ids": []}]},
    )
    job = await _job(deps)
    resp = await generate_legal_draft(job, LegalDraftRequest(jurisdiction="EU", acknowledgements=ACKS), deps)
    assert "VeriLoop's verdict is MANIPULATED" in resp.draft_text and "Made up." not in resp.draft_text


async def test_redacted_job_uses_url_placeholder(deps: Deps) -> None:
    deps.llm = DisabledLLM()
    view = await _job(deps)
    redacted = view.model_copy(
        update={"input": view.input.model_copy(update={"url": None}), "redacted": True}
    )
    resp = await generate_legal_draft(
        redacted, LegalDraftRequest(jurisdiction="US", acknowledgements=ACKS), deps
    )
    assert "[CONTENT URL]" in resp.placeholders
