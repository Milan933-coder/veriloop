"""The fixed set of external tools (BUILD_SPEC §4, D-15) and their resilience metadata.

Names are a closed set: ToolClient rejects unknown names so /health, chaos and the audit
`tool_versions` always cover every integration. Defaults live here; `config/tools.yaml` may override
the tunables (timeout_s, cache_ttl_s, retries, secondary, version) without code changes.

`secondary` is advisory: ToolClient does not call it automatically (the fallback has a different
request shape). Services use `ToolClient.secondary_for(tool)` to pick the next provider (spec:
retry x2 -> secondary provider -> mark branch insufficient).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_settings: tuple[str, ...] = ()  # Settings field names that must be non-empty
    timeout_s: float = 10.0  # per attempt
    cache_ttl_s: int = 0  # 0 = never cached
    retries: int = 2  # extra attempts after the first (D-12: retry x2)
    secondary: tuple[str, ...] = ()
    version: str = ""  # recorded in audit tool_versions (G7); LLM versions are set at runtime

    @property
    def keyless(self) -> bool:
        return not self.required_settings


_H = 3600
_DAY = 86400

_DEFAULTS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "factcheck",
        "Google Fact Check Tools API (claims:search, claims:imageSearch)",
        ("google_factcheck_api_key",),
        6.0,
        _DAY,
        2,
        ("claimreview_feed",),
        "factchecktools v1alpha1",
    ),
    ToolSpec(
        "claimreview_feed",
        "Local Postgres index of Google's public ClaimReview feed (D-15)",
        (),
        5.0,
        0,
        1,
        (),
        "datacommons claimreview feed",
    ),
    ToolSpec(
        "gnews_rss", "Google News RSS search", (), 6.0, 30 * 60, 2, ("bing_news_rss",), "news.google.com rss"
    ),
    ToolSpec(
        "bing_news_rss", "Bing News RSS search", (), 6.0, 30 * 60, 2, ("gnews_rss",), "bing.com/news rss"
    ),
    ToolSpec(
        "wire_rss",
        "Wire coverage via Google News RSS site: filters (D-09)",
        (),
        6.0,
        30 * 60,
        2,
        ("bing_news_rss",),
        "news.google.com rss site:wire",
    ),
    ToolSpec("pib_rss", "Press Information Bureau (India) RSS", (), 6.0, 30 * 60, 2, (), "pib.gov.in rss"),
    ToolSpec("gdelt", "GDELT DOC 2.0 API", (), 10.0, _H, 2, (), "gdelt doc 2.0"),
    ToolSpec(
        "serper",
        "Serper general search (/search, /news)",
        ("serper_api_key",),
        6.0,
        _H,
        2,
        ("gnews_rss", "bing_news_rss"),
        "serper v1",
    ),
    ToolSpec(
        "wikipedia", "Wikipedia search (MediaWiki action API)", (), 6.0, _DAY, 2, (), "mediawiki action api"
    ),
    ToolSpec("wikidata", "Wikidata SPARQL / wbsearchentities", (), 8.0, _DAY, 2, (), "wdqs sparql"),
    ToolSpec(
        "vision_web",
        "Google Cloud Vision WEB_DETECTION",
        ("google_vision_api_key",),
        10.0,
        _DAY,
        2,
        (),
        "vision v1 web_detection",
    ),
    ToolSpec(
        "sightengine",
        "Sightengine deepfake + genai models",
        ("sightengine_api_user", "sightengine_api_secret"),
        20.0,
        _DAY,
        2,
        (),
        "sightengine 1.0",
    ),
    ToolSpec(
        "hf_inference", "Hugging Face Inference API", ("hf_token",), 20.0, _DAY, 2, (), "hf inference api"
    ),
    # LLM attempts are expensive (up to LLM_TIMEOUT_S each): one retry keeps worst case ~2x timeout.
    ToolSpec(
        "llm_primary",
        "aicredits.in primary model (LLM_PRIMARY_MODEL)",
        ("llm_api_key",),
        20.0,
        0,
        1,
        ("llm_secondary",),
        "",
    ),
    ToolSpec(
        "llm_secondary",
        "aicredits.in second judge (LLM_SECONDARY_MODEL)",
        ("llm_api_key",),
        20.0,
        0,
        1,
        (),
        "",
    ),
    ToolSpec("oembed", "Platform oEmbed endpoints", (), 6.0, _H, 2, ("page_fetch",), "oembed 1.0"),
    ToolSpec(
        "page_fetch", "Plain HTTP page fetch + meta parsing", (), 8.0, _H, 2, ("playwright_fetch",), "httpx"
    ),
    ToolSpec("ytdlp", "yt-dlp metadata / media download", (), 60.0, 0, 1, ("playwright_fetch",), "yt-dlp"),
    ToolSpec(
        "playwright_fetch",
        "Headless Chromium fetch (fallback for JS/login-walled pages)",
        (),
        30.0,
        0,
        1,
        (),
        "playwright chromium",
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in _DEFAULTS)
LLM_TOOLS: frozenset[str] = frozenset({"llm_primary", "llm_secondary"})

_OVERRIDABLE = {"timeout_s", "cache_ttl_s", "retries", "secondary", "version"}


def default_registry() -> dict[str, ToolSpec]:
    return {spec.name: spec for spec in _DEFAULTS}


def apply_overrides(registry: Mapping[str, ToolSpec], overrides: Mapping[str, Any]) -> dict[str, ToolSpec]:
    """Apply `{tool: {timeout_s: .., cache_ttl_s: .., ...}}`. Unknown tools/fields fail loudly."""
    out = dict(registry)
    for name, fields in (overrides or {}).items():
        if name not in out:
            raise ValueError(f"tools config: unknown tool {name!r}")
        fields = dict(fields or {})
        unknown = set(fields) - _OVERRIDABLE
        if unknown:
            raise ValueError(f"tools config: {name}: unknown field(s) {sorted(unknown)}")
        if "secondary" in fields:
            secondary = tuple(fields["secondary"] or ())
            bad = [s for s in secondary if s not in out]
            if bad:
                raise ValueError(f"tools config: {name}: unknown secondary tool(s) {bad}")
            fields["secondary"] = secondary
        for key in ("timeout_s",):
            if key in fields:
                fields[key] = float(fields[key])
        for key in ("cache_ttl_s", "retries"):
            if key in fields:
                fields[key] = int(fields[key])
        out[name] = replace(out[name], **fields)
    return out


def load_registry(path: Path | None = None, *, llm_timeout_s: float | None = None) -> dict[str, ToolSpec]:
    """Defaults + optional YAML overrides (`tools:` mapping). LLM timeouts follow LLM_TIMEOUT_S."""
    registry = default_registry()
    if path is not None and path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        registry = apply_overrides(registry, data.get("tools") or {})
    if llm_timeout_s is not None:
        for name in LLM_TOOLS:
            registry[name] = replace(registry[name], timeout_s=float(llm_timeout_s))
    return registry
