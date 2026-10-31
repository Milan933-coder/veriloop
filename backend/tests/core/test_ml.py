"""Embedder/NLI behaviour without models (default run) + one real load each (`-m ml`)."""

import time

import numpy as np
import pytest

from veriloop.core.config import Settings, default_home
from veriloop.core.errors import MLUnavailable
from veriloop.core.ml import NLI, Embedder, label_index
from veriloop.core.testing import FakeEmbedder, FakeNLI


async def test_disabled_models_report_and_raise(settings: Settings) -> None:
    emb, nli = Embedder(settings), NLI(settings)
    assert not emb.available and emb.status() == "disabled"
    assert not nli.available and nli.status() == "disabled"
    assert await emb.load() is False
    with pytest.raises(MLUnavailable, match="ML_ENABLE=false"):
        await emb.embed(["x"])
    with pytest.raises(MLUnavailable):
        await nli.score([("a", "b")])
    assert (await emb.embed([])).shape == (0, 384)  # empty input never needs the model
    assert await nli.score([]) == []


async def test_load_failure_degrades_to_unavailable(make_settings, monkeypatch) -> None:
    emb = Embedder(make_settings(ml_enable=True))
    if emb.status() == "unavailable":  # libraries not installed in this env: already degraded
        assert not emb.available
        return

    def boom() -> None:
        raise OSError("download failed")

    monkeypatch.setattr(emb, "_load_sync", boom)
    assert emb.available and emb.status() == "idle"
    assert await emb.load() is False
    assert emb.status() == "unavailable" and not emb.available and "download failed" in (emb.error or "")
    with pytest.raises(MLUnavailable, match="download failed"):
        await emb.embed(["x"])


def test_label_index_reads_config_order() -> None:
    idx = label_index({0: "entailment", 1: "neutral", 2: "contradiction"})
    assert (idx.entailment, idx.neutral, idx.contradiction) == (0, 1, 2)
    idx = label_index({"0": "CONTRADICTION", "1": "NEUTRAL", "2": "ENTAILMENT"})
    assert (idx.entailment, idx.neutral, idx.contradiction) == (2, 1, 0)
    with pytest.raises(ValueError):
        label_index({0: "LABEL_0", 1: "LABEL_1"})


async def test_fakes() -> None:
    emb = FakeEmbedder()
    v = await emb.embed(["same", "same", "other"], kind="passage")
    assert v.shape == (3, 384) and np.allclose(np.linalg.norm(v, axis=1), 1.0)
    assert np.allclose(v[0], v[1]) and not np.allclose(v[0], v[2])
    nli = FakeNLI()
    (s,) = await nli.score([("p", "h")])
    assert s.neutral == 0.8 and nli.calls == [("p", "h")]
    with pytest.raises(MLUnavailable):
        await FakeEmbedder(available=False).embed(["x"])


@pytest.fixture
def ml_settings(make_settings) -> Settings:
    # Real VERILOOP_HOME so the weights download once and are reused across runs.
    return make_settings(ml_enable=True, veriloop_home=default_home())


@pytest.mark.ml
@pytest.mark.timeout(1800)  # first run downloads weights (~1 GB total)
async def test_real_embedder_smoke(ml_settings: Settings) -> None:
    emb = Embedder(ml_settings)
    t0 = time.perf_counter()
    assert await emb.load(), emb.error
    load_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    q = await emb.embed(["Did the PM inaugurate the new bridge in Mumbai?"], kind="query")
    p = await emb.embed(
        [
            "The Prime Minister inaugurated the Atal Setu sea bridge in Mumbai on Friday.",
            "मुंबई में प्रधानमंत्री ने नए पुल का उद्घाटन किया।",
            "Heavy rain is expected in Chennai this weekend.",
        ],
        kind="passage",
    )
    call_s = time.perf_counter() - t1
    assert q.shape == (1, 384) and p.shape == (3, 384) and q.dtype == np.float32
    assert np.allclose(np.linalg.norm(p, axis=1), 1.0, atol=1e-4)
    sims = (p @ q[0]).tolist()
    assert sims[0] > sims[2] and sims[1] > sims[2]
    print(f"\nEMBEDDER {emb.model_id}: load {load_s:.1f}s, 4 texts {call_s * 1000:.0f} ms, sims={sims}")


@pytest.mark.ml
@pytest.mark.timeout(1800)  # first run downloads weights (~1 GB total)
async def test_real_nli_smoke(ml_settings: Settings) -> None:
    nli = NLI(ml_settings)
    t0 = time.perf_counter()
    assert await nli.load(), nli.error
    load_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    scores = await nli.score(
        [
            (
                "The Prime Minister inaugurated the Atal Setu bridge in Mumbai on Friday.",
                "A bridge opened in Mumbai.",
            ),
            (
                "The Prime Minister inaugurated the Atal Setu bridge in Mumbai on Friday.",
                "No bridge was opened in Mumbai.",
            ),
            ("प्रधानमंत्री ने शुक्रवार को मुंबई में अटल सेतु पुल का उद्घाटन किया।", "A bridge opened in Mumbai."),
        ]
    )
    call_s = time.perf_counter() - t1
    assert scores[0].entailment > scores[0].contradiction
    assert scores[1].contradiction > scores[1].entailment
    assert scores[2].entailment > scores[2].contradiction
    print(
        f"\nNLI {nli.model_id}: labels={nli.labels}, load {load_s:.1f}s, 3 pairs {call_s * 1000:.0f} ms, "
        f"scores={[s.model_dump() for s in scores]}"
    )
