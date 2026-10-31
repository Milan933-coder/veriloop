"""Provenance pure helpers: event dates, out-of-context score, origins, context lists, manifest parsing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.services.ingestion.helpers import make_image
from veriloop.contracts import Appearance, Claim, ClaimExtraction
from veriloop.services.provenance import index, seeding, sources, timeline, web

NOW = datetime(2026, 10, 31, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Cyclone Arnab hit Visakhapatnam on 28 October 2026", datetime(2026, 10, 28, tzinfo=UTC)),
        ("on October 28, 2026", datetime(2026, 10, 28, tzinfo=UTC)),
        ("2026-09-15 collision", datetime(2026, 9, 15, tzinfo=UTC)),
        ("15/09/2026", datetime(2026, 9, 15, tzinfo=UTC)),  # India: day first
        ("in Sept 2026", datetime(2026, 9, 1, tzinfo=UTC)),
        ("missile hits Tel Aviv tonight", datetime(2026, 10, 30, tzinfo=UTC)),
        ("during the 2019 Pulwama attack", datetime(2019, 1, 1, tzinfo=UTC)),
        ("no date here", None),
    ],
)
def test_parse_event_date(text: str, expected: datetime | None) -> None:
    assert timeline.parse_event_date(text, NOW) == expected


def extraction(text: str, claim_type: str = "event", time_reference: str | None = None) -> ClaimExtraction:
    claim = Claim(id="C1", text=text, text_en=text, claim_type=claim_type, time_reference=time_reference)
    return ClaimExtraction(claims=[claim], primary_claim_id="C1")


def test_claimed_event_date_explicit_and_implicit() -> None:
    assert timeline.claimed_event_date(extraction("x", time_reference="28 October 2026"), NOW) == (
        datetime(2026, 10, 28, tzinfo=UTC),
        True,
    )
    assert timeline.claimed_event_date(extraction("Video shows a cyclone hitting the city"), NOW) == (
        NOW,
        False,
    )
    assert timeline.claimed_event_date(extraction("GDP is 5%", claim_type="statistic"), NOW) == (None, False)
    assert timeline.claimed_event_date(ClaimExtraction(), NOW) == (None, False)


def test_out_of_context_score() -> None:
    event = datetime(2026, 10, 28, tzinfo=UTC)
    assert timeline.out_of_context_score(None, event, explicit=True) == 0.0
    assert (
        timeline.out_of_context_score(datetime(2026, 10, 25, tzinfo=UTC), event, explicit=True) == 0.0
    )  # < 7 d
    july = timeline.out_of_context_score(datetime(2026, 7, 6, tzinfo=UTC), event, explicit=True)
    assert july == 1.0  # 114 days earlier: real footage, wrong event
    assert timeline.out_of_context_score(datetime(2026, 7, 6, tzinfo=UTC), event, explicit=False) == 0.7
    assert 0.5 < timeline.out_of_context_score(datetime(2026, 10, 1, tzinfo=UTC), event, explicit=True) < 0.8


def test_earliest_ignores_undated() -> None:
    a = Appearance(
        url="https://a.example", match_type="phash", first_seen_at=datetime(2026, 7, 6, tzinfo=UTC)
    )
    b = Appearance(
        url="https://b.example", match_type="phash", first_seen_at=datetime(2026, 10, 1, tzinfo=UTC)
    )
    c = Appearance(url="https://c.example", match_type="page")
    assert timeline.earliest([b, c, a]) is a and timeline.earliest([c]) is None


def test_origin_tags_and_variant_ids() -> None:
    note = "Typhoon Bavi footage " + index.format_origin(
        "https://www.youtube.com/watch?v=XeYAGU1Z6l8", "2026-07-06"
    )
    url, when = index.parse_origin(note)
    assert url == "https://www.youtube.com/watch?v=XeYAGU1Z6l8" and when == datetime(2026, 7, 6, tzinfo=UTC)
    assert index.strip_origin(note) == "Typhoon Bavi footage"
    assert index.base_id("KF-004~demo-b-f03") == "KF-004" and index.base_id("KF-004") == "KF-004"


def test_context_lists() -> None:
    satire = sources.context_for("https://www.theonion.com/some-story")
    assert [h.flag for h in satire] == ["satire"] and "satire.json" in satire[0].source
    assert [h.flag for h in sources.context_for("https://www.rt.com/news/1")] == ["state_affiliated"]
    assert [h.flag for h in sources.context_for("https://postcard.news/x")] == ["known_disinfo"]
    assert sources.context_for("https://www.thehindu.com/news") == []
    assert (
        sources.registrable_domain("https://english.factcrescendo.com/2026/10/28/x/") == "factcrescendo.com"
    )


def test_read_manifest() -> None:
    entries = {e.id: e for e in seeding.read_manifest()}
    assert len(entries) == 20
    kf1 = entries["KF-001"]
    assert kf1.label == "manipulated" and len(kf1.factcheck_urls) == 4
    assert kf1.factcheck_urls[0].startswith("https://www.boomlive.in/fact-check/")
    assert kf1.first_seen == datetime(2026, 10, 20, tzinfo=UTC)
    kf3 = entries["KF-003"]
    assert kf3.raw_label == "ai_generated" and kf3.label == "manipulated"  # DB has no ai_generated label
    assert kf3.db_notes.startswith("Manifest label: ai_generated.")
    assert len(kf3.alt_image_urls()) == 2 and len(entries["KF-004"].alt_image_urls()) == 1
    assert entries["KF-004"].label == "out_of_context"
    assert seeding.extension_for(kf1.media_url, kf1.media_type) == ".jpg"
    assert seeding.extension_for("https://a.b/x.webp?w=1", None) == ".webp"


def test_image_variants_full_and_crop(settings, tmp_path: Path) -> None:
    path = make_image(tmp_path / "kf.jpg", size=(400, 300), seed=11)
    images = seeding.image_variants_blocking(path, "KF-T", settings, use_clip=False)
    assert [i.variant_id for i in images] == ["KF-T", "KF-T~crop70"]
    assert images[0].sha256 and images[1].sha256 is None  # a crop's bytes never circulate
    assert images[0].phash != images[1].phash and all(i.clip is None for i in images)


def test_flat_images_are_not_indexed(settings, tmp_path: Path) -> None:
    from PIL import Image

    path = tmp_path / "black.png"
    Image.new("RGB", (200, 200)).save(path)
    assert seeding.image_variants_blocking(path, "KF-X", settings, use_clip=False) == []


def test_parse_vision_and_image_search() -> None:
    pages, labels = web.parse_vision(
        {
            "responses": [
                {
                    "webDetection": {
                        "pagesWithMatchingImages": [
                            {
                                "url": "https://a.example/1",
                                "pageTitle": "<b>Typhoon</b> Bavi",
                                "fullMatchingImages": [{}],
                            },
                            {"url": "https://b.example/2", "partialMatchingImages": [{}]},
                            {"url": "https://c.example/3"},
                            {"pageTitle": "no url"},
                        ],
                        "bestGuessLabels": [{"label": "typhoon"}],
                    }
                }
            ]
        }
    )
    assert [(p.url, p.match_type) for p in pages] == [
        ("https://a.example/1", "full"),
        ("https://b.example/2", "partial"),
        ("https://c.example/3", "page"),
    ]
    assert pages[0].title == "Typhoon Bavi" and labels == ["typhoon"]
    reviews = web.parse_image_search(
        {
            "results": [
                {
                    "claim": {
                        "text": "Video shows Cyclone Arnab",
                        "claimReview": [
                            {
                                "url": "https://newschecker.in/fc/1",
                                "publisher": {"name": "Newschecker"},
                                "textualRating": "False",
                                "reviewDate": "2026-10-28T00:00:00Z",
                            }
                        ],
                    }
                }
            ]
        }
    )
    assert reviews[0].url == "https://newschecker.in/fc/1" and reviews[0].rating_text == "False"
