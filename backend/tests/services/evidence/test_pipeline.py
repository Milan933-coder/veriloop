"""gather_evidence / followup_search end to end against respx mocks (no network) + the ClaimReview index."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from veriloop.contracts import (
    AbstainReason,
    BranchStatus,
    Claim,
    ClaimExtraction,
    Entity,
    EvidenceState,
    RetrievalMethod,
    Stance,
    VerdictLabel,
)
from veriloop.core.ids import new_job_id
from veriloop.core.llm import DisabledLLM
from veriloop.db import repo
from veriloop.services.evidence import followup_search, gather_evidence
from veriloop.services.evidence import sources as src
from veriloop.services.fusion import decide, fuse

from ..fusion.factories import constant_calibration, forensic, provenance
from .helpers import BowEmbedder, bing_rss, gnews_rss

DA_CLAIM = "The Union Cabinet approved a 3% hike in Dearness Allowance for central government employees."
HINDU_URL = (
    "https://www.thehindu.com/news/national/cabinet-approves-3-hike-in-dearness-allowance/article1.ece"
)
PIB_URL = "https://pib.gov.in/PressReleaseIframePage.aspx?PRID=2329484"


def ext(
    text: str = DA_CLAIM, *, language: str = "en", text_en: str | None = None, entities=()
) -> ClaimExtraction:
    return ClaimExtraction(
        language=language,
        claims=[Claim(id="C1", text=text, text_en=text_en, entities=list(entities))],
        primary_claim_id="C1",
        check_worthiness=0.9,
    )


def stance_by_keyword(keyword: str, stance: str):
    """FakeLLM stance responder: items whose TEXT contains `keyword` get `stance` with an exact quote."""

    def respond(call):
        out = []
        for block in call.user.split("\n\n")[1:]:
            m = re.match(r"\[(\w+)\]", block)
            text = re.search(r"TEXT: (.*)", block)
            if not m or not text:
                continue
            body = text.group(1)
            if keyword.lower() in body.lower():
                out.append({"id": m.group(1), "stance": stance, "confidence": 0.9, "quote": body[:30]})
            else:
                out.append({"id": m.group(1), "stance": "neutral", "confidence": 0.5, "quote": ""})
        return {"items": out}

    return respond


def _decode_routes(mock: respx.MockRouter, mapping: dict[str, str]) -> None:
    def article(request: httpx.Request) -> httpx.Response:
        gid = request.url.path.rsplit("/", 1)[-1]
        if gid not in mapping:
            return httpx.Response(404)
        return httpx.Response(200, text=f'<div data-n-a-sg="sig{gid}" data-n-a-ts="1730000000"></div>')

    def batch(request: httpx.Request) -> httpx.Response:
        body = parse_qs(request.content.decode())["f.req"][0]
        gid = next(g for g in mapping if g in body)
        return httpx.Response(
            200, text=f')]}}\'\n[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"{mapping[gid]}\\",1]"]]'
        )

    mock.route(method="GET", host="news.google.com", path__startswith="/rss/articles/").mock(
        side_effect=article
    )
    mock.route(method="POST", host="news.google.com", path="/_/DotsSplashUi/data/batchexecute").mock(
        side_effect=batch
    )


@pytest.fixture(autouse=True)
def _reset_gdelt_budget(monkeypatch):
    monkeypatch.setattr(src, "_gdelt_last_call", 0.0)


async def test_gather_evidence_end_to_end(deps, fake_llm, db):
    deps.embedder = BowEmbedder()
    fake_llm.script("stance", stance_by_keyword("approve", "support"))
    gnews_items = [
        (
            "Cabinet approves 3% hike in Dearness Allowance for central government employees",
            "The Hindu",
            "https://www.thehindu.com",
            "AAA",
        ),
        (
            "Cabinet approves 3% DA hike for central government employees, allowance rises to 63%",
            "Moneycontrol",
            "https://www.moneycontrol.com",
            "BBB",
        ),
        ("Easy paneer tikka recipe for the weekend", "Food Blog", "https://foodblog.example", "CCC"),
    ]
    bing_items = [
        (
            "Cabinet approves 3% DA hike for central government employees",
            "The Union Cabinet approved a 3% hike in dearness allowance for central government employees.",
            "https://www.financialexpress.com/money/cabinet-approves-3-da-hike-4371335/",
            "Financial Express",
        ),
        (
            "Cabinet approves 3% DA hike for central government employees and pensioners",
            "Dearness allowance for central government employees raised by 3%.",
            "https://www.msn.com/en-in/money/news/da-hike/ar-AA1",
            "The Hindu on MSN",
        ),
    ]
    pib_xml = (
        "<rss><channel><item><title>Cabinet approves 3% increase in Dearness Allowance for central government"
        f" employees</title><link>{PIB_URL}</link></item></channel></rss>"
    )
    wiki = {
        "query": {
            "pages": [
                {
                    "index": 1,
                    "title": "Dearness allowance",
                    "fullurl": "https://en.wikipedia.org/wiki/Dearness_allowance",
                    "extract": "Dearness allowance is a cost of living adjustment for central staff.",
                }
            ]
        }
    }

    def gnews(request: httpx.Request) -> httpx.Response:
        if "site:reuters.com" in request.url.params["q"]:
            return httpx.Response(500)  # wire_rss outage -> DEGRADED + secondary bing
        return httpx.Response(200, text=gnews_rss(gnews_items))

    with respx.mock(assert_all_called=False) as mock:
        mock.route(method="GET", host="news.google.com", path="/rss/search").mock(side_effect=gnews)
        _decode_routes(mock, {"AAA": HINDU_URL})
        bing = mock.route(method="GET", host="www.bing.com").mock(
            return_value=httpx.Response(200, text=bing_rss(bing_items))
        )
        mock.route(method="GET", host="www.pib.gov.in").mock(return_value=httpx.Response(200, text=pib_xml))
        mock.route(method="GET", host="en.wikipedia.org").mock(return_value=httpx.Response(200, json=wiki))
        mock.route(method="GET", host="api.gdeltproject.org").mock(
            return_value=httpx.Response(200, text="Please limit requests to one every 5 seconds")
        )
        job_id = new_job_id()
        await repo.create_job(db, job_id)
        result = await gather_evidence(
            ext(entities=[Entity(name="Union Cabinet", type="org")]), None, "en-IN", deps, job_id=job_id
        )

    assert result.branch == "evidence" and result.status is BranchStatus.DEGRADED
    errors = {(e.tool, e.error_type) for e in result.errors}
    assert ("wire_rss", "http_error") in errors
    assert any(e.tool == "gdelt" for e in result.errors)
    assert any("secondary bing_news_rss used" in n for n in result.notes)
    assert any(
        q.startswith("Union") or "Cabinet" in q for q in [r.request.url.params["q"] for r in bing.calls]
    )
    assert (
        "factcheck: unconfigured (GOOGLE_FACTCHECK_API_KEY not set); keyless ClaimReview feed used"
        in result.notes
    )
    assert any(n.startswith("serper: unconfigured") for n in result.notes)
    assert {
        "gnews_rss",
        "bing_news_rss",
        "wire_rss",
        "pib_rss",
        "wikipedia",
        "gdelt",
        "claimreview_feed",
    } <= set(result.tools_used)

    items = result.items
    assert [i.id for i in items] == [f"E{n}" for n in range(1, len(items) + 1)]
    returned = {HINDU_URL, PIB_URL, "https://en.wikipedia.org/wiki/Dearness_allowance"}
    returned |= {f"https://news.google.com/rss/articles/{g}?oc=5" for *_, g in gnews_items}
    returned |= {u for _, _, u, _ in bing_items}
    assert all(i.url in returned for i in items)  # URL validator: nothing a tool did not return
    assert not any("paneer" in (i.title or "") for i in items)  # irrelevant item ranked out

    by_title = {i.url: i for i in items}
    hindu = by_title[HINDU_URL]  # decoded Google News link
    assert hindu.domain == "thehindu.com" and hindu.publisher_group == "the_hindu_group"
    msn = next(i for i in items if "msn.com" in i.url)
    assert msn.publisher_group == "the_hindu_group"  # "The Hindu on MSN" collapses into The Hindu
    pib = by_title[PIB_URL]
    assert pib.is_government_source and pib.publisher_group == "govt_india" and pib.published_at is None
    supported = [i for i in items if i.stance is Stance.SUPPORT]
    assert len({i.publisher_group for i in supported}) >= 3
    assert all(i.stance_quote and i.stance_quote in i.snippet for i in supported)

    stored = await repo.list_evidence(db, job_id)
    assert [i.id for i in stored] == [i.id for i in items]

    fusion = fuse(
        ext(), result, forensic(), provenance(), deps.settings, calibration=constant_calibration(0.9)
    )
    assert fusion.candidate is VerdictLabel.TRUE and fusion.groups_support >= 3
    verdict = decide(ext(), fusion, None, forensic(), provenance(), items, deps.settings, llm_enabled=False)
    assert verdict.label is VerdictLabel.TRUE and verdict.independent_source_count >= 2


async def test_total_outage_is_insufficient_and_never_raises(deps, fake_llm):
    deps.embedder = BowEmbedder()
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=httpx.ConnectError("offline"))
        result = await gather_evidence(ext(), None, "en-IN", deps)
    assert result.status is BranchStatus.INSUFFICIENT and result.items == []
    failed = {e.tool for e in result.errors}
    assert {"gnews_rss", "bing_news_rss", "wire_rss"} <= failed
    fusion = fuse(ext(), result, forensic(), provenance(), deps.settings)
    # G8: "could not look" is not "nothing exists", and neither is "refuted"
    assert fusion.candidate_reason is AbstainReason.TOOL_OUTAGE
    assert fusion.evidence_state is EvidenceState.NO_EVIDENCE


async def test_unexpected_crash_becomes_failed_branch(deps, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("bug")

    monkeypatch.setattr("veriloop.services.evidence.pipeline.plan_queries", boom)
    result = await gather_evidence(ext(), None, "en-IN", deps)
    assert result.status is BranchStatus.FAILED and result.errors[0].tool == "node:evidence"


async def test_no_claim_is_insufficient(deps):
    result = await gather_evidence(ClaimExtraction(), None, "en-IN", deps)
    assert result.status is BranchStatus.INSUFFICIENT and result.notes == ["no claim to search for"]


# ---- fact-check fast path: feed index + JSON-LD page + headline verdict ------------------------------
CYCLONE = "This video shows Cyclone Arnab hitting Visakhapatnam in Andhra Pradesh"
FACTLY_URL = "https://factly.in/old-video-linked-to-cyclone-arnab-visakhapatnam/"
PAGE = """<html><head><script type="application/ld+json">{"@type":"ClaimReview",
"claimReviewed":"Video shows Cyclone Arnab hitting Visakhapatnam in Andhra Pradesh",
"author":{"name":"Factly"},"reviewRating":{"alternateName":"Misleading"}}</script></head></html>"""


async def test_claimreview_feed_jsonld_and_headline_verdicts(deps, fake_llm, db):
    deps.embedder = BowEmbedder()
    review_url = "https://english.factcrescendo.com/2026/10/28/old-video-cyclone-arnab-visakhapatnam/"
    await repo.upsert_claimreviews(
        db,
        [
            repo.ClaimReviewRecord(
                claim_text="Video shows Cyclone Arnab hitting Visakhapatnam in Andhra Pradesh",
                review_url=review_url,
                publisher_name="Fact Crescendo",
                publisher_site="factcrescendo.com",
                rating_text="Fact Crescendo Rating: Misleading",
            ),
            repo.ClaimReviewRecord(
                claim_text="Zebra crossing paint is made of chocolate in Visakhapatnam",
                review_url="https://example-fc.org/zebra",
                rating_text="False",
            ),
        ],
    )
    fake_llm.script("stance", stance_by_keyword("cyclone", "refute"))
    gnews_items = [
        (
            "Old video falsely shared as Cyclone Arnab hitting Visakhapatnam",
            "FACTLY",
            "https://factly.in",
            "FFF",
        ),
        (
            "Fact Check: Old video from US territory linked to Cyclone Arnab in Visakhapatnam",
            "Newschecker",
            "https://newschecker.in",
            "NNN",
        ),
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.route(method="GET", host="news.google.com", path="/rss/search").mock(
            return_value=httpx.Response(200, text=gnews_rss(gnews_items))
        )
        _decode_routes(mock, {"FFF": FACTLY_URL})
        mock.route(method="GET", host="factly.in").mock(return_value=httpx.Response(200, text=PAGE))
        mock.route(method="GET", host="www.bing.com").mock(
            return_value=httpx.Response(200, text=bing_rss([]))
        )
        mock.route(method="GET", host="en.wikipedia.org").mock(return_value=httpx.Response(200, json={}))
        mock.route(method="GET", host="api.gdeltproject.org").mock(
            return_value=httpx.Response(200, text="{}")
        )
        mock.route(method="GET", host="news.google.com", path__startswith="/rss/articles/NNN").mock(
            return_value=httpx.Response(404)
        )
        result = await gather_evidence(ext(CYCLONE), None, "en-IN", deps)

    feed = [i for i in result.items if i.tool == "claimreview_feed"]
    assert [i.url for i in feed] == [review_url]  # the unrelated review is not matched
    assert feed[0].retrieval_method is RetrievalMethod.FACTCHECK_CLAIMSEARCH and feed[0].id == "E1"
    assert feed[0].claimreview.similarity >= 0.6 and feed[0].claimreview.normalized_rating == -0.6
    factly = next(i for i in result.items if i.url == FACTLY_URL)
    assert factly.is_fact_check and factly.claimreview.rating_text == "Misleading"  # JSON-LD
    newschecker = next(i for i in result.items if i.domain == "newschecker.in")
    assert newschecker.claimreview.rating_text.endswith("(detected in headline)")
    fusion = fuse(
        ext(CYCLONE), result, forensic(), provenance(), deps.settings, calibration=constant_calibration(0.1)
    )
    assert fusion.groups_refute >= 2 and fusion.candidate is VerdictLabel.FALSE
    assert fusion.evidence_state is EvidenceState.REFUTES


async def test_hindi_claim_searches_both_languages(deps, fake_llm):
    deps.embedder = BowEmbedder()
    deps.llm = DisabledLLM()
    hindi = "केंद्रीय मंत्रिमंडल ने महंगाई भत्ते में 3 प्रतिशत की बढ़ोतरी को मंजूरी दी"
    with respx.mock(assert_all_called=False) as mock:
        g = mock.route(method="GET", host="news.google.com", path="/rss/search").mock(
            return_value=httpx.Response(200, text=gnews_rss([]))
        )
        b = mock.route(method="GET", host="www.bing.com").mock(
            return_value=httpx.Response(200, text=bing_rss([]))
        )
        mock.route().mock(return_value=httpx.Response(200, text="{}"))
        result = await gather_evidence(
            ext(hindi, language="hi", text_en="The Union Cabinet approved a 3% DA hike"), None, "hi-IN", deps
        )
    gl = [(c.request.url.params["hl"], c.request.url.params["q"]) for c in g.calls]
    assert any(hl == "hi" and "मंत्रिमंडल" in q for hl, q in gl)
    assert any(hl == "en-IN" and "Cabinet" in q for hl, q in gl)
    assert any(c.request.url.params["mkt"] == "hi-IN" for c in b.calls)
    assert result.status is BranchStatus.INSUFFICIENT  # nothing found: absence, not refutation


async def test_followup_search_returns_d_items(deps):
    deps.embedder = BowEmbedder()
    bing_items = [
        (
            "Cabinet approves 3% DA hike",
            "The Union Cabinet approved a 3% DA hike.",
            "https://www.financialexpress.com/money/x/",
            "Financial Express",
        ),
        ("Unrelated cricket score", "India won the match.", "https://sports.example/x", "Sports"),
    ]
    with respx.mock(assert_all_called=False) as mock:
        mock.route(method="GET", host="news.google.com").mock(
            return_value=httpx.Response(200, text=gnews_rss([]))
        )
        mock.route(method="GET", host="www.bing.com").mock(
            return_value=httpx.Response(200, text=bing_rss(bing_items))
        )
        items = await followup_search("Cabinet approves 3% DA hike", deps, start_index=4)
    assert [i.id for i in items] == ["D4"]
    d = items[0]
    assert d.branch == "debate" and d.retrieval_method is RetrievalMethod.FOLLOWUP_SEARCH
    assert d.stance is Stance.UNKNOWN and d.url == "https://www.financialexpress.com/money/x/"
    assert await followup_search("   ", deps) == []


def test_gnews_rss_helper_is_valid_json_free_xml():
    # guard for the helper itself: titles with "&" must be escaped
    assert "&amp;" in gnews_rss([("A & B", "P", "https://p.example", "X")])
    assert json.loads("{}") == {}
