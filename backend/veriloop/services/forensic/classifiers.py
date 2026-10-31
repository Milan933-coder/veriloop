"""Local Hugging Face image classifiers -> P(fake) per image. Loaded once, pinned to a revision.

The label that means "fake" is read from each model's own config (id2label), never assumed by index;
sigmoid models (one logit) are read as P(fake) per their model card.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import structlog
from PIL import Image

from veriloop.core.config import Settings

from .models_config import ModelSpec

log = structlog.get_logger(__name__)

BATCH = 16


class LocalClassifier:
    def __init__(self, spec: ModelSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        self._model: Any = None
        self._processor: Any = None
        self._fake_indices: list[int] = []
        self._error: str | None = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self.load_ms: int | None = None

    @property
    def state(self) -> str:
        if self._model is not None:
            return "loaded"
        return "unavailable" if self._error else "idle"

    @property
    def error(self) -> str | None:
        return self._error

    def load(self) -> bool:
        with self._load_lock:
            if self._model is not None:
                return True
            if self._error is not None:
                return False
            started = time.perf_counter()
            try:
                import torch  # noqa: F401  (fail early with a clear message if torch is missing)
                from transformers import AutoImageProcessor, AutoModelForImageClassification

                cache = str(self.settings.models_dir / "hf" / "hub")
                kwargs = {"revision": self.spec.revision, "cache_dir": cache}
                processor = AutoImageProcessor.from_pretrained(self.spec.model_id, **kwargs)
                model = AutoModelForImageClassification.from_pretrained(self.spec.model_id, **kwargs)
                model.eval()
                fake_indices: list[int] = []
                if self.spec.output == "softmax":
                    labels = {int(k): str(v).lower() for k, v in model.config.id2label.items()}
                    fake_indices = sorted(i for i, name in labels.items() if name in self.spec.fake_labels)
                    # Every configured fake label must exist: a renamed upstream label must fail loudly.
                    if len(fake_indices) != len(self.spec.fake_labels):
                        raise ValueError(f"cannot find the fake label(s) {self.spec.fake_labels} in {labels}")
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("forensic_model_unavailable", model=self.spec.model_id, error=self._error)
                return False
            self._model, self._processor, self._fake_indices = model, processor, fake_indices
            self.load_ms = int((time.perf_counter() - started) * 1000)
            log.info("forensic_model_loaded", model=self.spec.model_id, load_ms=self.load_ms)
            return True

    def predict(self, images: list[Image.Image]) -> list[float]:
        """P(fake) for each image, in order. Raises RuntimeError if the model is unavailable."""
        if not images:
            return []
        if not self.load():
            raise RuntimeError(self._error or "model unavailable")
        import torch

        out: list[float] = []
        with self._infer_lock, torch.inference_mode():
            for start in range(0, len(images), BATCH):
                chunk = [im.convert("RGB") for im in images[start : start + BATCH]]
                inputs = self._processor(images=chunk, return_tensors="pt")
                logits = self._model(**inputs).logits.float()
                if self.spec.output == "sigmoid":
                    probs = torch.sigmoid(logits[:, 0])
                else:  # P(fake) = total probability of the fake classes (e.g. "ai" + "deepfake")
                    probs = torch.softmax(logits, dim=-1)[:, self._fake_indices].sum(dim=-1)
                out.extend(float(p) for p in probs.cpu().numpy().astype(np.float64))
        return out


_registry: dict[str, LocalClassifier] = {}
_registry_lock = threading.Lock()


def get_classifier(spec: ModelSpec, settings: Settings) -> LocalClassifier:
    with _registry_lock:
        clf = _registry.get(spec.model_id)
        if clf is None:
            clf = _registry[spec.model_id] = LocalClassifier(spec, settings)
        return clf


def status() -> dict[str, str]:
    return {model_id: clf.state for model_id, clf in _registry.items()}
