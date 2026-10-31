from pathlib import Path

import pytest

from veriloop.core import config as config_mod
from veriloop.core.config import ENV_FILE, REPO_ROOT, Settings


def _write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_env_file_is_located_relative_to_package_not_cwd() -> None:
    assert (REPO_ROOT / "backend" / "veriloop" / "core" / "config.py").is_file()
    assert ENV_FILE == REPO_ROOT / ".env"
    assert config_mod.Settings.model_config["env_file"] == ENV_FILE


def test_loads_values_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LLM_PROVIDER", "LLM_API_KEY", "LLM_ALLOWED_MODELS", "TOOL_MODE", "ABSTAIN_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    env = _write_env(
        tmp_path,
        "LLM_API_KEY=sk-test-123456\nLLM_ALLOWED_MODELS=gpt-6-luna, 5.6-luna\nTOOL_MODE=replay\n"
        "ABSTAIN_THRESHOLD=0.8\nSERPER_API_KEY=\n",
    )
    s = Settings(_env_file=env)  # type: ignore[call-arg]
    assert s.llm_api_key.get_secret_value() == "sk-test-123456"
    assert s.allowed_models == ["gpt-6-luna", "5.6-luna"]
    assert s.tool_mode == "replay"
    assert s.abstain_threshold == 0.8
    assert not s.is_set("serper_api_key")  # `KEY=` means unset
    assert "sk-test-123456" not in repr(s)  # secrets never print


def test_process_env_overrides_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _write_env(tmp_path, "TOOL_MODE=replay\nLLM_TIMEOUT_S=20\n")
    monkeypatch.setenv("TOOL_MODE", "record")
    monkeypatch.setenv("LLM_TIMEOUT_S", "7.5")
    s = Settings(_env_file=env)  # type: ignore[call-arg]
    assert s.tool_mode == "record"
    assert s.llm_timeout_s == 7.5


def test_llm_provider_auto_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with_key = Settings(_env_file=None, llm_api_key="sk-abcdef123456")  # type: ignore[call-arg]
    assert with_key.llm_provider == "aicredits" and with_key.llm_provider_auto
    without = Settings(_env_file=None)  # type: ignore[call-arg]
    assert without.llm_provider == "none" and not without.llm_enabled
    explicit = Settings(_env_file=None, llm_provider="none", llm_api_key="sk-abcdef123456")  # type: ignore[call-arg]
    assert explicit.llm_provider == "none" and not explicit.llm_provider_auto


def test_defaults_match_build_spec(settings: Settings) -> None:
    assert settings.llm_base_url == "https://api.aicredits.in/v1"
    assert settings.llm_primary_model == "gpt-6-luna"
    assert settings.llm_secondary_model == "5.6-luna"
    assert settings.max_concurrent_jobs == 6
    assert settings.frame_cap == 16
    assert settings.embed_model == "intfloat/multilingual-e5-small"
    assert settings.nli_model == "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"


def test_media_dir_created_and_traversal_rejected(settings: Settings) -> None:
    path = settings.media_dir("01abcxyz")
    assert path.is_dir() and path.parent == settings.home / "media"
    for bad in ("../etc", "a/b", "", "..", "a\\b"):
        with pytest.raises(ValueError):
            settings.media_dir(bad)


def test_mode_labels(make_settings) -> None:
    assert make_settings().mode_labels() == ["LLM disabled — extractive rationale"]
    assert make_settings(
        tool_mode="replay", llm_provider="aicredits", llm_api_key="k" * 10
    ).mode_labels() == ["REPLAY MODE"]


def test_secret_values_lists_configured_secrets(make_settings) -> None:
    s = make_settings(
        llm_api_key="sk-secret-value-1", serper_api_key="serper-xyz-123", api_keys="key-one1,key-two2"
    )
    values = s.secret_values()
    assert {"sk-secret-value-1", "serper-xyz-123", "key-one1", "key-two2"} <= set(values)
    assert "change-me" not in values


def test_models_dir_defaults_under_home_and_can_be_overridden(make_settings, tmp_path) -> None:
    assert make_settings().models_dir == make_settings().home / "models"
    assert make_settings(ml_models_dir=tmp_path / "weights").models_dir == tmp_path / "weights"
