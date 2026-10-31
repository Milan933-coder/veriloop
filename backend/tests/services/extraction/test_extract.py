"""extract_claims with FakeLLM / DisabledLLM (no network, ML off): claims, E3, E9, D-16, visual queries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.services.ingestion.helpers import make_image
from veriloop.contracts import InputKind, MediaAsset, MediaKind, Platform, ResolvedInput
from veriloop.core.llm import DisabledLLM
from veriloop.core.testing import FakeEmbedder
from veriloop.deps import Deps
from veriloop.services.extraction import VISUAL_QUERY_PREFIX, asr, extract_claims, visual_queries
from veriloop.services.extraction.service import VISUAL_NOTE, _mismatch
from veriloop.services.ingestion.imaging import describe_image

CAPTION = "This video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh."


def resolved(caption: str | None, *, media: list[MediaAsset] | None = None) -> ResolvedInput:
    return ResolvedInput(
        input_kind=InputKind.URL if media else InputKind.TEXT,
        platform=Platform.YOUTUBE if media else Platform.TEXT,
        caption_text=caption,
        media=media or [],
    )


def llm_out(**overrides: Any) -> dict[str, Any]:
    out = {
        "language": "en",
        "claims": [
            {
                "text": "Video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh",
                "text_en": "Video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh",
                "claim_type": "event",
                "entities": [
                    {"name": "Cyclone Arnab", "type": "event"},
                    {"name": "Visakhapatnam", "type": "place"},
                ],
                "time_reference": None,
                "location": "Visakhapatnam",
            }
        ],
        "primary_index": 0,
        "check_worthiness": 0.92,
        "worthiness_reason": "Viral disaster video claim",
        "subject": {"is_identifiable_individual": False, "is_private_individual": None, "name": None},
    }
    out.update(overrides)
    return out


async def test_llm_extraction(deps: Deps, fake_llm) -> None:
    fake_llm.script("extract_claims", llm_out())
    out = await extract_claims(resolved(CAPTION), "en-IN", deps)
    assert out.llm_used and out.language == "en" and out.check_worthiness == 0.92
    claim = out.primary_claim()
    assert claim is not None and claim.id == "C1" and claim.location == "Visakhapatnam"
    assert [e.type for e in claim.entities] == ["event", "place"]
    call = fake_llm.calls_for("extract_claims")[0]
    assert call.effort == "none" and CAPTION in call.user  # content is handed over, never knowledge


async def test_ungrounded_llm_claims_are_dropped(deps: Deps, fake_llm) -> None:
    """D-16: a claim that is not in the content (model 'knowledge') never becomes a claim."""
    extra = {
        "text": "Typhoon Bavi made landfall on Rota island in July 2026",
        "text_en": "Typhoon Bavi made landfall on Rota island in July 2026",
        "claim_type": "event",
        "entities": [],
        "time_reference": "July 2026",
        "location": "Rota",
    }
    fake_llm.script("extract_claims", llm_out(claims=[*llm_out()["claims"], extra]))
    out = await extract_claims(resolved(CAPTION), "en-IN", deps)
    assert [c.text for c in out.claims] == ["Video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh"]
    assert any(n.startswith("dropped ungrounded LLM claim") for n in out.notes)


async def test_hindi_claim_gets_english_translation(deps: Deps, fake_llm) -> None:
    """E9: language detected, original text kept, English rendering provided for search."""
    hindi = "केंद्रीय मंत्रिमंडल ने महंगाई भत्ते में 3 प्रतिशत की बढ़ोतरी को मंजूरी दी।"
    fake_llm.script(
        "extract_claims",
        llm_out(
            language="hi",
            claims=[
                {
                    "text": hindi,
                    "text_en": "The Union Cabinet approved a 3 percent hike in dearness allowance.",
                    "claim_type": "statistic",
                    "entities": [],
                    "time_reference": None,
                    "location": None,
                }
            ],
        ),
    )
    out = await extract_claims(resolved(hindi), "hi-IN", deps)
    assert out.language == "hi"
    claim = out.primary_claim()
    assert (
        claim is not None and claim.text == hindi and claim.text_en and claim.text_en.startswith("The Union")
    )


async def test_llm_disabled_uses_heuristics(deps: Deps) -> None:
    deps.llm = DisabledLLM()
    out = await extract_claims(resolved(CAPTION + " Share before it is deleted!"), "en-IN", deps)
    assert not out.llm_used and out.claims and out.check_worthiness >= 0.4
    assert out.primary_claim().text == CAPTION  # type: ignore[union-attr]
    assert "LLM disabled — extractive claim split" in out.notes


async def test_llm_failure_falls_back(deps: Deps, fake_llm) -> None:
    # Nothing scripted: FakeLLM raises ToolCallFailed like a real outage.
    out = await extract_claims(resolved(CAPTION), "en-IN", deps)
    assert not out.llm_used and out.claims
    assert any("extractive fallback" in n for n in out.notes)


async def test_recipe_is_not_checkable_e3(deps: Deps) -> None:
    deps.llm = DisabledLLM()
    text = "Easy paneer recipe! Ingredients: 200 grams of paneer, 2 tbsp butter. Simmer 10 minutes, garnish."
    out = await extract_claims(resolved(text), "en-IN", deps)
    assert out.check_worthiness <= 0.15 and out.worthiness_reason.startswith("Not checkable: cooking")


async def test_non_english_without_llm_is_flagged_e9(deps: Deps) -> None:
    deps.llm = DisabledLLM()
    text = "ब्रेकिंग: इज़राइल पर अभी-अभी ईरानी मिसाइलों से हमला हुआ है। वीडियो देखें 2026"
    out = await extract_claims(resolved(text), "hi-IN", deps)
    assert out.language == "hi"
    assert any("no translation available" in n for n in out.notes)
    assert all(c.text_en is None for c in out.claims)


async def test_subject_from_llm(deps: Deps, fake_llm) -> None:
    caption = "Video: US Senator Richard Blumenthal says India is a failed state."
    fake_llm.script(
        "extract_claims",
        llm_out(
            claims=[
                {
                    "text": caption,
                    "text_en": caption,
                    "claim_type": "quote",
                    "entities": [{"name": "Richard Blumenthal", "type": "person"}],
                    "time_reference": None,
                    "location": None,
                }
            ],
            subject={
                "is_identifiable_individual": True,
                "is_private_individual": False,
                "name": "Richard Blumenthal",
            },
        ),
    )
    out = await extract_claims(resolved(caption), "en-IN", deps)
    assert out.subject.is_identifiable_individual and out.subject.is_private_individual is False
    assert out.subject.name == "Richard Blumenthal"


async def test_visual_queries_are_marked_as_queries(deps: Deps, fake_llm, tmp_path: Path) -> None:
    path = make_image(tmp_path / "f.jpg", seed=5)
    info = describe_image(path)
    frame = MediaAsset(
        id="M1", kind=MediaKind.VIDEO_FRAME, path=str(path), sha256=info.sha256, phash=info.phash
    )
    fake_llm.script("extract_claims", llm_out())
    fake_llm.script(
        "visual_queries",
        {
            "scene": "Palm trees bending in a storm next to a road",
            "visible_text": ["NDT"],
            "landmarks": [],
            "public_figures": [],
            "search_queries": ["typhoon palm trees road storm video", "NDT typhoon footage"],
        },
    )
    out = await extract_claims(resolved(CAPTION, media=[frame]), "en-IN", deps)
    assert visual_queries(out) == ["typhoon palm trees road storm video", "NDT typhoon footage"]
    assert VISUAL_NOTE in out.notes and all(
        q.startswith(VISUAL_QUERY_PREFIX) for q in out.notes if "typhoon" in q
    )
    call = fake_llm.calls_for("visual_queries")[0]
    assert call.effort == "none" and call.images and call.images[0].startswith("data:image/jpeg;base64,")


async def test_mismatch_signal(deps: Deps) -> None:
    spoken = asr.Transcript("the senator praised india economy growth today", "en", 0.9, 10, "small", 1.0)
    deps.embedder = FakeEmbedder()
    same = await _mismatch(spoken, "the senator praised india economy growth today", deps, [])
    other = await _mismatch(spoken, "breaking cyclone hits the coast tonight with heavy rain", deps, [])
    assert same == 0.0 and other is not None and other > same
    assert await _mismatch(spoken, "short", deps, []) is None  # too thin to compare


async def test_never_raises(deps: Deps, monkeypatch) -> None:
    from veriloop.services.extraction import service

    async def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(service, "_extract", boom)
    out = await extract_claims(resolved(CAPTION), "en-IN", deps)
    assert out.claims and any("extraction error" in n for n in out.notes)
