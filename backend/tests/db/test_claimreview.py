from datetime import UTC, datetime

import numpy as np

from veriloop.core.ids import new_job_id
from veriloop.core.testing import FakeEmbedder
from veriloop.db import repo
from veriloop.db.engine import Database
from veriloop.db.repo import ClaimReviewRecord


def record(tag: str, claim: str, **kw) -> ClaimReviewRecord:
    return ClaimReviewRecord(
        claim_text=claim,
        review_url=f"https://factcheck.example/{tag}",
        publisher_name="Example Check",
        publisher_site="factcheck.example",
        rating_text="False",
        rating_value=1,
        best_rating=5,
        worst_rating=1,
        language=kw.pop("language", "en"),
        review_date=datetime(2026, 9, 1, tzinfo=UTC),
        item_appearance_urls=[f"https://x.com/post/{tag}"],
        **kw,
    )


async def test_bulk_upsert_fts_embeddings_and_stats(db: Database) -> None:
    tag = new_job_id()  # unique words so this test only sees its own rows
    recs = [
        record(
            f"{tag}-1", f"Video shows zebracrossing{tag} bridge collapse in Mumbai", claimant="Viral post"
        ),
        record(f"{tag}-2", f"Photo of zebracrossing{tag} flood in Chennai is from 2015"),
        record(f"{tag}-3", f"मुंबई में पुल{tag} गिरा", language="hi"),
    ]
    assert recs[0].id == repo.claimreview_id(recs[0].review_url, recs[0].claim_text)
    dup = recs[0].model_copy(update={"rating_text": "Misleading"})
    assert (
        await repo.upsert_claimreviews(db, [*recs, dup], batch_size=2) == 4
    )  # 2 batches; the dup (same id) updates the first row
    assert (await repo.get_claimreview(db, recs[0].id)).rating_text == "Misleading"

    hits = await repo.search_claimreviews_fts(db, f"the zebracrossing{tag} bridge collapsed in Mumbai?")
    assert [h.record.id for h in hits][:2] == [recs[0].id, recs[1].id]  # more shared words ranks higher
    assert hits[0].rank > hits[1].rank and hits[0].embedding is None
    assert hits[0].record.item_appearance_urls == [f"https://x.com/post/{tag}-1"]
    hi_hits = await repo.search_claimreviews_fts(db, f"पुल{tag}", language="hi")
    assert [h.record.id for h in hi_hits] == [recs[2].id]
    assert await repo.search_claimreviews_fts(db, "the is a of") == []

    missing = {m.id for m in await repo.claimreviews_missing_embeddings(db, limit=100000)}
    assert {r.id for r in recs} <= missing
    emb = FakeEmbedder()
    vecs = await emb.embed([r.claim_text for r in recs], kind="passage")
    assert (
        await repo.set_claimreview_embeddings(db, [(r.id, v) for r, v in zip(recs, vecs, strict=True)]) == 3
    )
    assert not {r.id for r in recs} & {
        m.id for m in await repo.claimreviews_missing_embeddings(db, limit=100000)
    }

    q = (await emb.embed([recs[1].claim_text]))[0]
    reranked = await repo.search_claimreviews_by_embedding(db, q, ids=[r.id for r in recs], limit=3)
    assert reranked[0].record.id == recs[1].id and abs(reranked[0].similarity - 1.0) < 1e-5
    fts_again = await repo.search_claimreviews_fts(db, f"zebracrossing{tag}")
    assert all(isinstance(h.embedding, np.ndarray) and h.embedding.shape == (384,) for h in fts_again)

    # changing the claim text invalidates the stored embedding; unchanged rows keep theirs
    changed = recs[1].model_copy(update={"claim_text": recs[1].claim_text})  # same id, same text
    await repo.upsert_claimreviews(db, [changed])
    assert recs[1].id not in {m.id for m in await repo.claimreviews_missing_embeddings(db, limit=100000)}
    edited = ClaimReviewRecord(**{**recs[2].model_dump(), "claim_text": recs[2].claim_text + " (संपादित)"})
    edited.id = recs[2].id
    await repo.upsert_claimreviews(db, [edited])
    assert recs[2].id in {m.id for m in await repo.claimreviews_missing_embeddings(db, limit=100000)}

    stats = await repo.claimreview_stats(db)
    assert stats.total >= 3 and stats.with_embedding >= 2 and stats.last_synced_at is not None


async def test_upsert_accepts_dicts_and_connections(db: Database) -> None:
    tag = new_job_id()
    async with db.acquire() as conn:
        n = await repo.upsert_claimreviews(
            conn, [{"claim_text": f"dict row {tag}", "review_url": f"https://e/{tag}"}]
        )
    assert n == 1
    assert len(await repo.search_claimreviews_fts(db, f"row {tag}")) == 1
