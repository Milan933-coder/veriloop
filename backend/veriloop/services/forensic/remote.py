"""Keyed forensic adapters (D-05, D-15): Sightengine (deepfake + genai) and HF Inference.

Both are fully implemented and auto-activate when their keys appear in `.env`; without keys ToolClient
returns `unconfigured` before any request is made. Secrets go only into the request, never into the
ToolClient key (cache/cassette identity), so cassettes stay secret-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veriloop.core.config import Settings
from veriloop.core.errors import ToolAttemptError
from veriloop.core.http import raise_for_status
from veriloop.core.tool_client import ToolCallResult, ToolClient

SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json"
SIGHTENGINE_MODELS = "deepfake,genai"
HF_INFERENCE_URL = "https://router.huggingface.co/hf-inference/models/{model_id}"


async def sightengine_check(
    tools: ToolClient, settings: Settings, image: bytes, sha256: str, filename: str = "frame.jpg"
) -> ToolCallResult:
    """POST one image to Sightengine check.json. Payload: the raw JSON response."""

    async def fn() -> Any:
        data = {
            "models": SIGHTENGINE_MODELS,
            "api_user": settings.sightengine_api_user.get_secret_value(),
            "api_secret": settings.sightengine_api_secret.get_secret_value(),
        }
        resp = await tools.http.post(
            SIGHTENGINE_URL, data=data, files={"media": (filename, image, "image/jpeg")}
        )
        raise_for_status(resp)
        body = resp.json()
        if body.get("status") != "success":
            err = body.get("error") or {}
            raise ToolAttemptError("http_error", f"sightengine: {err.get('type')}: {err.get('message')}")
        return body

    return await tools.call("sightengine", "check", {"sha256": sha256, "models": SIGHTENGINE_MODELS}, fn)


def parse_sightengine(body: Any) -> dict[str, float]:
    """{'deepfake': p, 'genai': p} from a check.json response (missing keys omitted)."""
    out: dict[str, float] = {}
    scores = (body or {}).get("type") or {}
    if isinstance(scores.get("deepfake"), int | float):
        out["deepfake"] = float(scores["deepfake"])
    if isinstance(scores.get("ai_generated"), int | float):
        out["genai"] = float(scores["ai_generated"])
    return out


async def hf_inference_classify(
    tools: ToolClient, settings: Settings, model_id: str, revision: str, image: bytes, sha256: str
) -> ToolCallResult:
    """Serverless HF Inference image-classification; payload: [{label, score}, ...]."""

    async def fn() -> Any:
        resp = await tools.http.post(
            HF_INFERENCE_URL.format(model_id=model_id),
            content=image,
            headers={
                "Authorization": f"Bearer {settings.hf_token.get_secret_value()}",
                "Content-Type": "image/jpeg",
            },
        )
        raise_for_status(resp)
        body = resp.json()
        if not isinstance(body, list):
            raise ToolAttemptError("parse_error", f"unexpected HF inference payload: {str(body)[:200]}")
        return body

    key = {"model": model_id, "revision": revision, "sha256": sha256}
    return await tools.call("hf_inference", "classify", key, fn)


def parse_hf_labels(body: Any, fake_labels: tuple[str, ...]) -> float | None:
    for item in body or []:
        if isinstance(item, dict) and str(item.get("label", "")).lower() in fake_labels:
            score = item.get("score")
            return float(score) if isinstance(score, int | float) else None
    return None


def read_bytes(path: str | Path) -> bytes:
    return Path(path).read_bytes()
