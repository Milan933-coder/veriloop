"""Pure parsers: news RSS / GDELT payloads, the ClaimReview DataFeed, fact-check page JSON-LD."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from veriloop.contracts import RetrievalMethod
from veriloop.services.evidence import rss
from veriloop.services.evidence.claimreview import apply_headline_rating, apply_page_rating, extract_page_data
from veriloop.services.evidence.feed_parse import (
    iter_feed_elements,
    parse_date,
    parse_element,
    script_language,
)
from veriloop.services.evidence.models import Candidate
from veriloop.services.evidence.sources import gdelt_query, gnews_article_id

from .helpers import bing_rss, gnews_rss


def test_parse_gnews_strips_publisher_suffix_and_keeps_source_url():
    xml = gnews_rss([("Cabinet approves 3% DA hike", "The Hindu", "https://www.thehindu.com", "CBMiAAA")])
    rows = rss.parse_gnews(xml)
    assert rows == [
        {
            "title": "Cabinet approves 3% DA hike",
            "link": "https://news.google.com/rss/articles/CBMiAAA?oc=5",
            "published_at": datetime(2026, 10, 28, 10, 0, tzinfo=UTC),
            "source_name": "The Hindu",
            "source_url": "https://www.thehindu.com",
        }
    ]
    assert gnews_article_id(rows[0]["link"]) == "CBMiAAA"
    assert gnews_article_id("https://example.com/rss/articles/x") is None


def test_parse_bing_uses_url_param_and_snippet():
    xml = bing_rss(
        [("DA hike", "Snippet text here.", "https://www.moneycontrol.com/news/a.html", "Moneycontrol")]
    )
    rows = rss.parse_bing(xml)
    assert rows[0]["url"] == "https://www.moneycontrol.com/news/a.html"
    assert rows[0]["description"] == "Snippet text here." and rows[0]["source_name"] == "Moneycontrol"
    assert rss.bing_target_url("https://www.bing.com/news/apiclick.aspx?url=javascript:x") is None


def test_parse_pib_title_and_link_only():
    xml = (
        "<rss><channel><item><title>Cabinet approves DA</title>"
        "<link>https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2329484</link></item>"
        "<item><title>bad</title><link>ftp://x</link></item></channel></rss>"
    )
    assert rss.parse_pib(xml) == [
        {"title": "Cabinet approves DA", "url": "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2329484"}
    ]


@pytest.mark.parametrize("xml", ["", "not xml at all", "<rss><channel></channel></rss>"])
def test_rss_parsers_tolerate_garbage(xml):
    assert rss.parse_gnews(xml) == [] and rss.parse_bing(xml) == [] and rss.parse_pib(xml) == []


def test_parse_gdelt_json_and_plain_text_errors():
    payload = json.dumps(
        {
            "articles": [
                {
                    "url": "https://indiagazette.com/x",
                    "title": "DA hike",
                    "seendate": "20261025T103000Z",
                    "domain": "indiagazette.com",
                    "language": "English",
                }
            ]
        }
    )
    rows = rss.parse_gdelt(payload)
    assert rows[0]["published_at"] == datetime(2026, 10, 25, 10, 30, tzinfo=UTC)
    assert rss.parse_gdelt("{}") == []
    with pytest.raises(rss.GdeltError):
        rss.parse_gdelt("Please limit requests to one every 5 seconds")
    with pytest.raises(rss.GdeltError):
        rss.parse_gdelt("The specified phrase is too short.")


def test_gdelt_query_quotes_hyphens_and_needs_two_terms():
    assert gdelt_query(["F-16", "Pakistan", "crash"]) == '"F-16" Pakistan crash'
    assert gdelt_query(["ab", "x"]) is None


# ---- ClaimReview DataFeed ---------------------------------------------------------------------
FEED = {
    "@context": "http://schema.org",
    "@type": "DataFeed",
    "dataFeedElement": [
        {"@type": "DataFeedItem", "url": "https://factly.in/a/", "item": None},
        {
            "@type": "DataFeedItem",
            "url": "https://factly.in/b/",
            "item": {  # a dict instead of a list (guarded)
                "@type": "ClaimReview",
                "url": "https://factly.in/b/",
                "claimReviewed": "  Video shows   cyclone in Vizag ",
                "author": {"name": "FACTLY", "url": "https://factly.in"},
                "datePublished": "710-01-01",
                "reviewRating": {"alternateName": "Misleading", "ratingValue": "2"},
                "itemReviewed": {
                    "author": {"name": "Social Media User"},
                    "datePublished": "2026-10-28",
                    "appearance": [{"url": "https://www.facebook.com/reel/1"}],
                },
            },
        },
        {
            "@type": "DataFeedItem",
            "url": "https://hindi.example/c",
            "item": [
                {"url": "https://hindi.example/c", "claimReviewed": "महंगाई भत्ता बढ़ा", "reviewRating": {}},
                {"url": "https://hindi.example/d"},  # no claimReviewed -> skipped
            ],
        },
    ],
}


def test_iter_feed_elements_streams_small_chunks(tmp_path, monkeypatch):
    from veriloop.services.evidence import feed_parse

    monkeypatch.setattr(feed_parse, "_CHUNK", 64)  # force elements to straddle chunk boundaries
    path = tmp_path / "data.json"
    path.write_text(json.dumps(FEED, ensure_ascii=False, indent=2), encoding="utf-8")
    elements = list(iter_feed_elements(path))
    assert len(elements) == 3
    records = [r for e in elements for r in parse_element(e)]
    assert len(records) == 2
    first = records[0]
    assert first.claim_text == "Video shows cyclone in Vizag"
    assert first.review_date is None  # garbage '710-01-01' rejected
    assert first.claim_date == datetime(2026, 10, 28, tzinfo=UTC)
    assert first.rating_text == "Misleading" and first.rating_value == 2.0
    assert first.publisher_name == "FACTLY" and first.publisher_site == "factly.in"
    assert first.item_appearance_urls == ["https://www.facebook.com/reel/1"]
    assert records[1].language == "hi" and records[1].rating_text is None


@pytest.mark.parametrize(
    ("value", "ok"),
    [("2026-10-30", True), ("2923-01-02", False), ("10-01-01", False), ("garbage", False), (None, False)],
)
def test_parse_date_validates_range(value, ok):
    assert (parse_date(value, now=datetime(2026, 10, 31, tzinfo=UTC)) is not None) is ok


def test_script_language():
    assert script_language("ఫేక్ వీడియో") == "te"
    assert script_language("Plain English") is None


# ---- fact-check pages (keyless ClaimReview recovery) ------------------------------------------
PAGE = """<html><head><meta property="og:title" content="Fact Check: Old video"/>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
 {"@type":"NewsArticle","datePublished":"2026-10-28T09:00:00+05:30"},
 {"@type":"ClaimReview","claimReviewed":"Video shows Cyclone Arnab hitting Visakhapatnam",
  "author":{"@type":"Organization","name":"Newschecker"},
  "reviewRating":{"@type":"Rating","alternateName":"Misleading"},
  "itemReviewed":{"@type":"Claim","author":{"@type":"Person","name":"Facebook user"}}}]}</script>
<script type="application/ld+json">{broken json</script></head></html>"""


def _cand(**kw) -> Candidate:
    base = dict(
        source_name="Newschecker",
        url="https://newschecker.in/fact-check/x",
        host="newschecker.in",
        title="Old video shared as Cyclone Arnab",
        snippet="Old video shared as Cyclone Arnab",
        retrieval_method=RetrievalMethod.GOOGLE_NEWS_RSS,
        tool="gnews_rss",
    )
    return Candidate(**{**base, **kw})


def test_extract_page_data_and_apply_rating():
    data = extract_page_data(PAGE)
    assert data["claimreviews"][0]["rating"] == "Misleading"
    assert data["claimreviews"][0]["claimant"] == "Facebook user"
    assert data["published"] == "2026-10-28T09:00:00+05:30" and data["title"] == "Fact Check: Old video"
    cand = _cand(published_at=datetime(2026, 7, 5, tzinfo=UTC))  # Bing's wrong date
    assert apply_page_rating(cand, data)
    assert cand.rating_method == "jsonld"
    assert cand.claimreview.normalized_rating == -0.6 and not cand.claimreview.manipulation_rating
    assert cand.published_at.date().isoformat() == "2026-10-28"  # page date wins


def test_headline_rating_when_no_jsonld():
    cand = _cand(title="Video of US Senator calling India a failed state is a deepfake")
    assert apply_page_rating(cand, {"claimreviews": [], "published": None}) is False
    assert apply_headline_rating(cand)
    assert cand.rating_method == "headline"
    assert cand.claimreview.manipulation_rating and cand.claimreview.normalized_rating == -1.0
    assert cand.claimreview.rating_text.endswith("(detected in headline)")
    plain = _cand(title="Cabinet approves 3% DA hike", snippet="Cabinet approves 3% DA hike")
    assert not apply_headline_rating(plain) and plain.claimreview is None
