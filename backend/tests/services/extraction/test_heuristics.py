"""Extractive fallback heuristics (LLM disabled): sentence split, worthiness, E3 non-claims, language."""

from __future__ import annotations

import pytest

from veriloop.contracts import MediaAsset, MediaKind
from veriloop.services.extraction import asr, heuristics, ocr


def test_split_sentences_drops_urls_hashtags_and_fragments() -> None:
    text = "Cyclone Arnab hits Visakhapatnam. https://t.co/x\n#viral #breaking\nWow!! Share this video now please."  # noqa: E501
    assert heuristics.split_sentences(text) == [
        "Cyclone Arnab hits Visakhapatnam.",
        "Share this video now please.",
    ]


def test_check_worthy_sentence_scores_high() -> None:
    s = heuristics.score_sentence(
        "The Union Cabinet approved a 3% hike in Dearness Allowance on 28 October 2026."
    )
    assert s.score >= 0.7
    assert {"event/assertion verb", "number", "date", "statistic", "named entity"} <= set(s.reasons)


@pytest.mark.parametrize(
    "text",
    [
        "Add two cups of flour, knead the dough and bake for 20 minutes until golden.",
        "Good morning everyone, have a great day!",
        "I think this is so beautiful omg",
        "Subscribe and follow us, link in bio for the giveaway",
        "Is this real?",
    ],
)
def test_non_claims_score_low(text: str) -> None:
    assert heuristics.score_sentence(text).score < 0.4


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            "Paneer masala recipe: ingredients 200 grams of paneer, 2 tbsp butter. Simmer and garnish.",
            "cooking/recipe content",
        ),
        ("Happy Diwali to all my friends!", "a greeting"),
    ],
)
def test_non_claim_reason_e3(text: str, reason: str) -> None:
    assert heuristics.non_claim_reason(text) == reason


def test_non_claim_reason_none_for_news() -> None:
    assert (
        heuristics.non_claim_reason("Video shows Cyclone Arnab hitting Visakhapatnam, Andhra Pradesh.")
        is None
    )


def test_detect_language() -> None:
    assert heuristics.detect_language("केंद्रीय मंत्रिमंडल ने महंगाई भत्ते में 3 प्रतिशत की बढ़ोतरी को मंजूरी दी।") == "hi"
    assert (
        heuristics.detect_language("The Union Cabinet approved a hike in dearness allowance today.") == "en"
    )
    assert heuristics.detect_language("") == "en" and heuristics.detect_language(None, default="xx") == "xx"


def test_entities_and_person_subject() -> None:
    text = "Video shows US Senator Richard Blumenthal calling India a failed state."
    assert "Richard Blumenthal" in heuristics.extract_entities(text)
    assert heuristics.person_subject(text) == "Richard Blumenthal"
    assert heuristics.person_subject("Floods hit the city") is None


def test_asr_model_choice_degrades_for_long_audio(settings) -> None:
    assert asr.choose_model(settings, 20.0) == "small"  # 20 s x 0.15 RTF = 3 s < 6 s budget
    assert asr.choose_model(settings, 90.0) == asr.FAST_MODEL  # 13.5 s predicted -> base


def test_pick_keyframes_prefers_distinct_frames() -> None:
    def asset(i: int, phash: str) -> MediaAsset:
        return MediaAsset(
            id=f"M{i}",
            kind=MediaKind.VIDEO_FRAME,
            path=f"f{i}.jpg",
            sha256="0" * 64,
            phash=phash,
            frame_ts=float(i),
        )

    same = "0" * 16
    frames = [asset(i, same) for i in range(6)] + [asset(6, "f" * 16), asset(7, "0f" * 8)]
    picked = ocr.pick_keyframes(frames, limit=4, min_distance=8)
    assert len({a.phash for a in picked}) == len(picked)  # no two near-identical frames
    assert len(picked) == 3
    assert ocr.pick_keyframes(frames[:2], limit=4) == frames[:2]


def test_ocr_language_choice() -> None:
    assert ocr.choose_langs("Cyclone hits the coast", None) == ocr.LANGS_EN
    assert ocr.choose_langs("चक्रवात ने तट पर दस्तक दी", None) == ocr.LANGS_EN_HI
    assert ocr.choose_langs("English caption", locale="hi-IN") == ocr.LANGS_EN_HI


def test_ocr_budget_reads_at_least_one_frame(settings, tmp_path, monkeypatch) -> None:
    from tests.services.ingestion.helpers import make_image

    class StubReader:
        def __init__(self) -> None:
            self.calls = 0

        def readtext(self, img, **_):  # type: ignore[no-untyped-def]
            self.calls += 1
            return [([(0, 0), (10, 0), (10, 10), (0, 10)], f"line {self.calls}", 0.9)]

    stub = StubReader()
    monkeypatch.setattr(ocr, "_get_reader", lambda settings, langs: stub)
    paths = [make_image(tmp_path / f"f{i}.jpg", seed=i) for i in range(3)]
    assert ocr.ocr_blocking(paths, settings, budget_s=0.0) == ["line 1"] and stub.calls == 1
    assert ocr.ocr_blocking(paths, settings, budget_s=60.0) == ["line 2", "line 3", "line 4"]


def test_wav_decoding_for_whisper(tmp_path) -> None:
    import wave

    import numpy as np

    path = tmp_path / "a.wav"
    samples = (np.sin(np.linspace(0, 100, 16000)) * 16000).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(samples.tobytes())
    audio = asr.load_wav_blocking(path)
    assert audio.dtype == np.float32 and audio.shape == (16000,)
    assert float(np.abs(audio).max()) <= 1.0 and asr.audio_seconds(path) == 1.0
