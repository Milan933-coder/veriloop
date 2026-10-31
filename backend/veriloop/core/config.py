"""Application settings (BUILD_SPEC §10).

Values come from, in priority order: explicit kwargs > process environment > the repo-root `.env`
> defaults. The `.env` is located relative to this file (not the CWD) so `python -m veriloop` works
from any directory; `VERILOOP_ENV_FILE` overrides the location (e.g. inside Docker).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parents[1]  # backend/veriloop
BACKEND_DIR = PACKAGE_DIR.parent
REPO_ROOT = BACKEND_DIR.parent
ENV_FILE = REPO_ROOT / ".env"

ToolMode = Literal["live", "record", "replay"]
LLMProvider = Literal["aicredits", "none"]

# Job ids and other path components must never escape their parent directory.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def default_home() -> Path:
    """VERILOOP_HOME default: outside the (OneDrive-synced) repo, in the user's home directory."""
    env = os.environ.get("VERILOOP_HOME")
    return Path(env).expanduser() if env else Path.home() / ".veriloop"


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # `KEY=` in .env means "use the default", not "empty string"
        extra="ignore",
        case_sensitive=False,
    )

    # ---- runtime / paths ---------------------------------------------------------------------
    veriloop_home: Path = Field(default_factory=default_home)
    database_url: str = ""  # empty -> embedded pgserver at VERILOOP_HOME/pgdata
    app_base_url: str = "http://localhost:8000"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True
    cassette_dir: Path = REPO_ROOT / "data" / "fixtures" / "cassettes"
    tools_config: Path = REPO_ROOT / "config" / "tools.yaml"
    cpu_workers: int = 0  # 0 -> auto (min(4, cpu_count))
    db_pool_min: int = 1
    db_pool_max: int = 10

    # ---- API ---------------------------------------------------------------------------------
    api_keys: SecretStr = SecretStr("")  # comma-separated
    admin_key: SecretStr = SecretStr("change-me")
    rate_limit_per_min: int = 20
    max_concurrent_jobs: int = 6

    # ---- LLM (aicredits.in only; D-01) -------------------------------------------------------
    llm_provider: LLMProvider | None = None  # None -> auto: aicredits if LLM_API_KEY else none
    llm_base_url: str = "https://api.aicredits.in/v1"
    llm_api_key: SecretStr = SecretStr("")
    llm_allowed_models: str = "gpt-6-luna,5.6-luna"
    llm_primary_model: str = "gpt-6-luna"
    llm_secondary_model: str = "5.6-luna"
    llm_second_judge: bool = True
    llm_timeout_s: float = 20.0

    # ---- keyed tools (all optional; D-15 keyless-first) ---------------------------------------
    google_factcheck_api_key: SecretStr = SecretStr("")
    google_vision_api_key: SecretStr = SecretStr("")
    serper_api_key: SecretStr = SecretStr("")
    sightengine_api_user: SecretStr = SecretStr("")
    sightengine_api_secret: SecretStr = SecretStr("")
    hf_token: SecretStr = SecretStr("")

    # ---- Telegram ----------------------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_secret: SecretStr = SecretStr("")
    telegram_ttl_hours: int = 24

    # ---- audit -------------------------------------------------------------------------------
    audit_ed25519_key: SecretStr = SecretStr("")  # PEM; empty -> generated under VERILOOP_HOME/keys

    # ---- tool flight recorder (D-12) ---------------------------------------------------------
    tool_mode: ToolMode = "live"

    # ---- decision thresholds -----------------------------------------------------------------
    abstain_threshold: float = 0.70
    checkworthiness_threshold: float = 0.40
    manipulation_min: float = 0.80
    forensic_low: float = 0.35
    forensic_high: float = 0.70
    forensic_delta: float = 0.40
    nli_entail_min: float = 0.50

    # ---- media limits ------------------------------------------------------------------------
    frame_fps: float = 1.0
    frame_cap: int = 16
    audio_cap_s: int = 90
    max_upload_mb: int = 50
    max_video_s: int = 900

    # ---- local ML ----------------------------------------------------------------------------
    ml_enable: bool = True
    ml_prewarm: bool = True
    asr_model: str = "small"
    embed_model: str = "intfloat/multilingual-e5-small"
    # Verified 2026-10-31 via https://huggingface.co/api/models/<id>; labels read from its config.
    nli_model: str = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"
    # Weights cache; empty -> VERILOOP_HOME/models. Point at another drive when C: is short on space.
    ml_models_dir: Path | None = None

    wikidata_user_agent: str = "VeriLoop/0.1 (hackathon; contact: team@veriloop.local)"

    _llm_provider_auto: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _resolve_llm_provider(self) -> Settings:
        # D-01b: an explicit provider wins; otherwise the presence of a key decides.
        if self.llm_provider is None:
            self.llm_provider = "aicredits" if self.llm_api_key.get_secret_value() else "none"
            self._llm_provider_auto = True
        return self

    # ---- derived values ----------------------------------------------------------------------
    @property
    def home(self) -> Path:
        return self.veriloop_home.expanduser()

    @property
    def pgdata_dir(self) -> Path:
        return self.home / "pgdata"

    @property
    def models_dir(self) -> Path:
        """Download cache for local model weights (kept out of the OneDrive-synced repo)."""
        return self.ml_models_dir.expanduser() if self.ml_models_dir else self.home / "models"

    @property
    def keys_dir(self) -> Path:
        return self.home / "keys"

    @property
    def llm_provider_auto(self) -> bool:
        """True when LLM_PROVIDER was not set and was derived from the presence of LLM_API_KEY."""
        return self._llm_provider_auto

    @property
    def allowed_models(self) -> list[str]:
        return _split_csv(self.llm_allowed_models)

    @property
    def api_key_list(self) -> list[str]:
        return _split_csv(self.api_keys.get_secret_value())

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider == "aicredits"

    def media_dir(self, job_id: str) -> Path:
        """Per-job media directory, created on demand. Rejects ids that could escape the media root."""
        if not _SAFE_COMPONENT.fullmatch(job_id):
            raise ValueError(f"unsafe job id for a path component: {job_id!r}")
        path = self.home / "media" / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_dirs(self) -> None:
        for path in (self.home, self.home / "media", self.models_dir, self.keys_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.apply_model_cache_env()

    def apply_model_cache_env(self) -> None:
        """Point third-party weight caches at models_dir.

        open_clip, faster-whisper, easyocr and torch.hub ignore our explicit cache paths unless told
        via env vars, and otherwise download into the user's C: profile (which ran out of space during
        the build). setdefault keeps an operator's explicit choice. Must run before those libraries
        are imported, so bootstrap calls it first.
        """
        for var, sub in (("HF_HOME", "hf"), ("TORCH_HOME", "torch"), ("EASYOCR_MODULE_PATH", "easyocr")):
            os.environ.setdefault(var, str(self.models_dir / sub))

    def mode_labels(self) -> list[str]:
        """User-visible caveats that apply to every job in this process (shown in UI + audit)."""
        labels: list[str] = []
        if self.tool_mode == "replay":
            labels.append("REPLAY MODE")
        elif self.tool_mode == "record":
            labels.append("RECORD MODE")
        if not self.llm_enabled:
            labels.append("LLM disabled — extractive rationale")
        return labels

    def is_set(self, field_name: str) -> bool:
        """True when a (possibly secret) setting has a non-empty value. Used for tool configuration."""
        value = getattr(self, field_name)
        if isinstance(value, SecretStr):
            return bool(value.get_secret_value().strip())
        return bool(value)

    def secret_values(self) -> list[str]:
        """Every configured secret value, so the log/cassette redactor can mask exact occurrences."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if name == "api_keys":
                    values.extend(_split_csv(raw))
                elif raw:
                    values.append(raw)
        # The default admin key is public knowledge; masking it would only add noise.
        return [v for v in values if v and v != "change-me"]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Tests construct `Settings(_env_file=None, ...)` instead."""
    env_file = os.environ.get("VERILOOP_ENV_FILE") or ENV_FILE
    return Settings(_env_file=env_file)  # type: ignore[call-arg]
