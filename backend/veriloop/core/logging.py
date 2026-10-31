"""structlog JSON logging with secret redaction.

Every log line (ours and stdlib/uvicorn/httpx lines routed through structlog) passes the redaction
processor, which masks: values under secret-looking keys (`authorization`, `*_api_key`, `*_token`...),
`Bearer ...` credentials, `sk-...` keys, Google `AIza...` keys, Telegram bot tokens, secret query
parameters (`?key=...`), and the exact values of every secret configured in Settings
(`register_secrets`). `redact()` is also used by the cassette recorder so cassettes never hold keys.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import IO, Any

import structlog

MASK = "***"

# Keys whose values are always secrets. Note `*_tokens` (token counts) must NOT match.
_SECRET_KEY_RE = re.compile(
    r"^(authorization|proxy-authorization|cookie|set-cookie|password|passwd|secret|token|api[_-]?key"
    r"|x-api-key|x-admin-key|access_token|refresh_token|client_secret|.*_api_key|.*_secret|.*_token"
    r"|.*-api-key|.*-token)$",
    re.IGNORECASE,
)
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 " + MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"), "sk-" + MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), MASK),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), MASK),  # Telegram bot token
    (
        re.compile(
            r"(?i)([?&](?:key|api_key|apikey|token|access_token|secret|api_secret|password)=)[^&\s\"'#]+"
        ),
        r"\1" + MASK,
    ),
)

_registered_secrets: set[str] = set()


def register_secrets(values: Iterable[str]) -> None:
    """Mask these exact values wherever they appear. Short values are ignored (false positives)."""
    for value in values:
        if value and len(value) >= 6:
            _registered_secrets.add(value)


def clear_registered_secrets() -> None:
    _registered_secrets.clear()


def redact_text(text: str) -> str:
    for secret in sorted(_registered_secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, MASK)
    for pattern, repl in _VALUE_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact(value: Any) -> Any:
    """Recursively redact strings, dict values under secret-looking keys, lists and tuples."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.match(key) and isinstance(item, str) and item:
                out[key] = MASK
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


def _redaction_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact(event_dict)


def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redaction_processor,
    ]


def configure_logging(level: str = "INFO", *, json_logs: bool = True, stream: IO[str] | None = None) -> None:
    """Configure structlog + stdlib logging once per process (idempotent; later calls reconfigure)."""
    shared = _shared_processors()
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Not cached: structlog.testing.capture_logs() must be able to intercept already-used loggers.
        cache_logger_on_first_use=False,
    )
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Route uvicorn through the root handler (JSON + redaction) instead of its own formatters.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # httpx logs every request URL at INFO; Google APIs carry `?key=` there. Keep it quiet.
    for name in ("httpx", "httpcore", "asyncio", "urllib3", "filelock", "huggingface_hub"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
