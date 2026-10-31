"""Speech-to-text with faster-whisper (CTranslate2, int8, CPU), loaded once per process.

Model choice per call: ASR_MODEL (default `small`) unless the predicted run time exceeds the budget
(~6 s), in which case `base` is used. The prediction is audio seconds x the measured real-time
factor of `small` on this machine (an exponential moving average updated after every run), so a
slow CPU or a long clip automatically degrades to the faster model.
"""

from __future__ import annotations

import os
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from veriloop.core.config import Settings

log = structlog.get_logger(__name__)

ASR_BUDGET_S = 6.0
SAMPLE_RATE = 16000
FAST_MODEL = "base"
_DEFAULT_RTF = {"tiny": 0.04, "base": 0.06, "small": 0.15, "medium": 0.4}

_models: dict[str, Any] = {}
_rtf: dict[str, float] = {}
_load_lock = threading.Lock()
_errors: dict[str, str] = {}


@dataclass
class Transcript:
    text: str
    language: str | None
    language_probability: float | None
    audio_s: float
    model: str
    seconds: float


def audio_seconds(path: str | Path) -> float:
    try:
        with wave.open(str(path), "rb") as wav:
            return wav.getnframes() / float(wav.getframerate() or 16000)
    except (wave.Error, OSError, EOFError):
        return 0.0


def load_wav_blocking(path: str | Path) -> np.ndarray:
    """16-bit PCM WAV (ingestion writes mono 16 kHz) -> float32 in [-1, 1].

    We decode ourselves instead of letting faster-whisper open the file: its PyAV path passes
    `metadata_errors=`, which PyAV >= 15 removed (TypeError with the av 19 in this venv).
    """
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {wav.getsampwidth() * 8}-bit")
        rate, channels = wav.getframerate(), wav.getnchannels()
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    audio = pcm.astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:  # linear resample; ingestion already delivers 16 kHz, this is a safety net
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


def choose_model(settings: Settings, audio_s: float) -> str:
    preferred = settings.asr_model
    if preferred == FAST_MODEL:
        return preferred
    predicted = audio_s * _rtf.get(preferred, _DEFAULT_RTF.get(preferred, 0.3))
    return FAST_MODEL if predicted > ASR_BUDGET_S else preferred


def _load(name: str, settings: Settings) -> Any:
    with _load_lock:
        if name in _models:
            return _models[name]
        if name in _errors:
            raise RuntimeError(_errors[name])
        started = time.perf_counter()
        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(
                name,
                device="cpu",
                compute_type="int8",
                download_root=str(settings.models_dir / "whisper"),
                cpu_threads=min(8, os.cpu_count() or 4),
            )
        except Exception as exc:
            _errors[name] = f"faster-whisper {name} unavailable: {type(exc).__name__}: {exc}"
            log.warning("asr_model_unavailable", model=name, error=_errors[name])
            raise RuntimeError(_errors[name]) from exc
        _models[name] = model
        log.info("asr_model_loaded", model=name, load_ms=int((time.perf_counter() - started) * 1000))
        return model


def load_blocking(settings: Settings, names: tuple[str, ...] | None = None) -> None:
    for name in names or (settings.asr_model, FAST_MODEL):
        try:
            _load(name, settings)
        except RuntimeError:
            pass


def transcribe_blocking(path: str | Path, settings: Settings) -> Transcript:
    audio_s = audio_seconds(path)
    name = choose_model(settings, audio_s)
    try:
        model = _load(name, settings)
    except RuntimeError:
        if name == FAST_MODEL:
            raise
        name, model = FAST_MODEL, _load(FAST_MODEL, settings)
    started = time.perf_counter()
    segments, info = model.transcribe(
        load_wav_blocking(path),
        beam_size=1,  # greedy: ~2x faster on CPU, negligible WER cost for short social clips
        vad_filter=True,  # skip music/silence (common in reels) instead of hallucinating text
        condition_on_previous_text=False,
    )
    text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
    seconds = time.perf_counter() - started
    if audio_s > 1:
        rtf = seconds / audio_s
        _rtf[name] = rtf if name not in _rtf else 0.7 * _rtf[name] + 0.3 * rtf
    return Transcript(
        text=" ".join(text.split()),
        language=getattr(info, "language", None),
        language_probability=getattr(info, "language_probability", None),
        audio_s=round(audio_s, 2),
        model=name,
        seconds=round(seconds, 2),
    )


def status() -> dict[str, str]:
    out = {f"faster-whisper/{n}": "loaded" for n in _models}
    out.update({f"faster-whisper/{n}": "unavailable" for n in _errors})
    return out
