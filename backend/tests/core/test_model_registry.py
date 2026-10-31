import pytest

from veriloop.core.errors import ModelNotAllowed, ModelResolutionError
from veriloop.core.model_registry import (
    ModelRegistry,
    matches_allowlist_entry,
    resolve_alias,
    resolve_from_listing,
)

LISTING = [
    {
        "id": "openai/gpt-6-luna",
        "is_active": True,
        "supported_apis": ["chat_completions"],
        "created": 1700000000,
    },
    {
        "id": "openai/gpt-5.6-luna",
        "is_active": True,
        "supported_apis": ["chat_completions"],
        "created": 1700000000,
    },
    {"id": "sao10k/l3-lunaris-8b", "is_active": True, "created": 1700000000},
    {"id": "openai/gpt-6-luna-mini", "is_active": True},
    {"id": "openai/gpt-4o", "is_active": True},
]


@pytest.mark.parametrize(
    ("model_id", "entry", "expected"),
    [
        ("openai/gpt-6-luna", "gpt-6-luna", True),
        ("openai/gpt-5.6-luna", "5.6-luna", True),
        ("gpt-5.6-luna", "5.6-luna", True),
        ("openai/gpt-6-luna", "5.6-luna", False),
        ("openai/gpt-5.6-luna", "gpt-6-luna", False),
        ("sao10k/l3-lunaris-8b", "5.6-luna", False),
        ("sao10k/l3-lunaris-8b", "gpt-6-luna", False),
        ("sao10k/l3-lunaris-8b", "luna", False),
        ("openai/gpt-6-luna-mini", "gpt-6-luna", False),
        ("openai/gpt-6-luna", "", False),
    ],
)
def test_allowlist_matching(model_id: str, entry: str, expected: bool) -> None:
    assert matches_allowlist_entry(model_id, entry) is expected


def test_resolve_from_listing(settings) -> None:
    resolved = resolve_from_listing(LISTING, settings)
    assert resolved.primary == "openai/gpt-6-luna"
    assert resolved.secondary == "openai/gpt-5.6-luna"
    assert resolved.candidates["5.6-luna"] == ["openai/gpt-5.6-luna"]


def test_inactive_or_non_chat_models_are_skipped() -> None:
    listing = [
        {"id": "openai/gpt-6-luna", "is_active": False},
        {"id": "other/gpt-6-luna", "is_active": True, "supported_apis": ["embeddings"]},
    ]
    assert resolve_alias(listing, "gpt-6-luna") is None


def test_resolution_prefers_exact_segment_then_shortest() -> None:
    listing = [{"id": "zz/vendor-gpt-6-luna"}, {"id": "aa/gpt-6-luna"}, {"id": "b/gpt-6-luna"}]
    assert resolve_alias(listing, "gpt-6-luna") == "b/gpt-6-luna"


def test_missing_primary_or_bad_config_raises(make_settings) -> None:
    with pytest.raises(ModelResolutionError):
        resolve_from_listing([{"id": "openai/gpt-4o"}], make_settings())
    with pytest.raises(ModelResolutionError):
        resolve_from_listing(LISTING, make_settings(llm_primary_model="gpt-4o"))  # not in allowlist
    missing_secondary = resolve_from_listing([LISTING[0]], make_settings())
    assert missing_secondary.secondary is None


def test_registry_only_allows_resolved_ids() -> None:
    reg = ModelRegistry(["openai/gpt-6-luna"])
    reg.assert_allowed("openai/gpt-6-luna")
    for bad in ("openai/gpt-4o", "gpt-6-luna", "sao10k/l3-lunaris-8b"):
        with pytest.raises(ModelNotAllowed):
            reg.assert_allowed(bad)
