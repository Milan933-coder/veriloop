"""Local ML models shared by services: multilingual embedder and multilingual NLI cross-encoder.

Both load lazily on first use (or `await x.load()` from a prewarm hook) on the shared CPU pool,
are loaded once per process, and degrade to `unavailable` (logged, never raised at import time)
when ML_ENABLE=false, the libraries are missing, or the download/load fails. Callers check
`.available` and handle `MLUnavailable`.

* Embedder: `intfloat/multilingual-e5-small` (384-d). e5 models are trained with `query: ` /
  `passage: ` prefixes; omitting them measurably hurts retrieval, so `embed(kind=...)` adds them.
  Output is L2-normalised float32, so cosine similarity == dot product (matches pgvector `<=>`).
* NLI: `MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7` (verified on the Hub
  2026-10-31; config id2label = {0: entailment, 1: neutral, 2: contradiction}). The label order is
  read from the downloaded config at load time, never assumed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import structlog
from pydantic import BaseModel, ConfigDict

from veriloop.core.config import Settings
from veriloop.core.errors import MLUnavailable
from veriloop.core.executor import run_cpu

log = structlog.get_logger(__name__)

MLState = Literal["disabled", "idle", "loading", "loaded", "unavailable"]
EMBED_DIM = 384


class NLIScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    entailment: float
    neutral: float
    contradiction: float


class _LazyModel:
    """Load-once wrapper: state machine idle -> loading -> loaded | unavailable."""

    kind = "model"
    required_modules: tuple[str, ...] = ()

    def __init__(self, settings: Settings, model_id: str) -> None:
        self.settings = settings
        self.model_id = model_id
        self._model: Any = None
        self._error: str | None = None
        self._lock: asyncio.Lock | None = None
        self.load_ms: int | None = None
        if not settings.ml_enable:
            self._state: MLState = "disabled"
        elif missing := [m for m in self.required_modules if importlib.util.find_spec(m) is None]:
            self._state = "unavailable"
            self._error = f"missing python package(s): {', '.join(missing)}"
            log.warning("ml_model_unavailable", kind=self.kind, model=model_id, error=self._error)
        else:
            self._state = "idle"

    @property
    def available(self) -> bool:
        """True if usable now or loadable on first use (False once disabled or failed)."""
        return self._state in ("idle", "loading", "loaded")

    @property
    def error(self) -> str | None:
        return self._error

    def status(self) -> str:
        return self._state

    def _hf_kwargs(self) -> dict[str, Any]:
        token = self.settings.hf_token.get_secret_value()
        return {"token": token} if token else {}

    def _load_sync(self) -> Any:
        raise NotImplementedError

    async def load(self) -> bool:
        """Load the model if needed. Returns availability; never raises for load failures."""
        if self._state == "loaded":
            return True
        if not self.available:
            return False
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._state == "loaded":
                return True
            if not self.available:
                return False
            self._state = "loading"
            started = time.perf_counter()
            try:
                self._model = await run_cpu(self._load_sync)
            except Exception as exc:
                self._state = "unavailable"
                self._error = f"{type(exc).__name__}: {exc}"[:500]
                log.warning("ml_model_unavailable", kind=self.kind, model=self.model_id, error=self._error)
                return False
            self.load_ms = int((time.perf_counter() - started) * 1000)
            self._state = "loaded"
            log.info("ml_model_loaded", kind=self.kind, model=self.model_id, load_ms=self.load_ms)
            return True

    async def _ensure(self) -> Any:
        if not await self.load():
            reason = "ML_ENABLE=false" if self._state == "disabled" else (self._error or self._state)
            raise MLUnavailable(f"{self.kind} {self.model_id} unavailable: {reason}")
        return self._model


class Embedder(_LazyModel):
    kind = "embedder"
    required_modules = ("sentence_transformers", "torch")
    dim = EMBED_DIM

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, settings.embed_model)

    def _load_sync(self) -> Any:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            self.model_id, device="cpu", cache_folder=str(self.settings.models_dir), **self._hf_kwargs()
        )
        dim = model.get_sentence_embedding_dimension()
        if dim != self.dim:
            raise ValueError(f"{self.model_id} produces {dim}-d vectors; the schema expects {self.dim}")
        return model

    async def embed(self, texts: Sequence[str], kind: Literal["query", "passage"] = "query") -> np.ndarray:
        """(n, 384) float32, L2-normalised. Raises MLUnavailable."""
        if kind not in ("query", "passage"):
            raise ValueError("kind must be 'query' or 'passage'")
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        model = await self._ensure()
        prefixed = [f"{kind}: {t}" for t in texts]
        vectors = await run_cpu(
            model.encode,
            prefixed,
            batch_size=32,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


@dataclass(frozen=True)
class LabelIndex:
    entailment: int
    neutral: int
    contradiction: int


def label_index(id2label: Mapping[Any, str]) -> LabelIndex:
    """Map a model config's id2label to our three labels (robust to order and naming variants)."""
    found: dict[str, int] = {}
    for idx, name in id2label.items():
        lowered = str(name).strip().lower()
        for label, prefix in (
            ("entailment", "entail"),
            ("neutral", "neutral"),
            ("contradiction", "contradict"),
        ):
            if lowered.startswith(prefix):
                found[label] = int(idx)
    missing = {"entailment", "neutral", "contradiction"} - found.keys()
    if missing:
        raise ValueError(f"NLI config id2label {dict(id2label)} lacks {sorted(missing)}")
    return LabelIndex(**found)


class NLI(_LazyModel):
    kind = "nli"
    required_modules = ("transformers", "torch")
    batch_size = 16
    max_length = 512

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings, settings.nli_model)
        self.labels: LabelIndex | None = None

    def _load_sync(self) -> Any:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        kwargs = {"cache_dir": str(self.settings.models_dir), **self._hf_kwargs()}
        tokenizer = AutoTokenizer.from_pretrained(self.model_id, **kwargs)
        model = AutoModelForSequenceClassification.from_pretrained(self.model_id, **kwargs)
        model.eval()
        self.labels = label_index(model.config.id2label)
        log.info("nli_label_order", model=self.model_id, labels=self.labels.__dict__)
        return tokenizer, model

    def _score_sync(self, pairs: list[tuple[str, str]]) -> list[NLIScore]:
        import torch

        tokenizer, model = self._model
        assert self.labels is not None
        out: list[NLIScore] = []
        for start in range(0, len(pairs), self.batch_size):
            batch = pairs[start : start + self.batch_size]
            enc = tokenizer(
                [p for p, _ in batch],
                [h for _, h in batch],
                truncation=True,
                max_length=self.max_length,
                padding=True,
                return_tensors="pt",
            )
            with torch.inference_mode():
                probs = torch.softmax(model(**enc).logits.float(), dim=-1).cpu().numpy()
            for row in probs:
                out.append(
                    NLIScore(
                        entailment=float(row[self.labels.entailment]),
                        neutral=float(row[self.labels.neutral]),
                        contradiction=float(row[self.labels.contradiction]),
                    )
                )
        return out

    async def score(self, pairs: Sequence[tuple[str, str]]) -> list[NLIScore]:
        """One NLIScore per (premise, hypothesis) pair. Raises MLUnavailable."""
        if not pairs:
            return []
        await self._ensure()
        return await run_cpu(self._score_sync, [(str(p), str(h)) for p, h in pairs])
