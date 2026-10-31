import io
import json
import logging

import structlog

from veriloop.core.logging import MASK, configure_logging, redact, redact_text, register_secrets


def test_redacts_bearer_sk_keys_query_params_and_registered_secrets() -> None:
    register_secrets(["supersecretvalue42"])
    text = (
        "Authorization: Bearer abcdefghijklmnop sk-1234567890abcdef "
        "https://x.googleapis.com/v1?key=AIzaSyA1234567890123456789012345678901&q=1 "
        "123456789:AAEhBOweik9ai2o4Ju5hnaPf9Plk4hpXyzA token supersecretvalue42"
    )
    out = redact_text(text)
    for leaked in ("abcdefghijklmnop", "1234567890abcdef", "AIzaSy", "AAEhBOweik", "supersecretvalue42"):
        assert leaked not in out
    assert "Bearer ***" in out and "q=1" in out


def test_redacts_secret_keys_but_not_token_counts() -> None:
    event = {
        "headers": {"Authorization": "Bearer xyzxyzxyzxyz", "Accept": "json"},
        "llm_api_key": "anything",
        "telegram_bot_token": "abc",
        "prompt_tokens": 12,
        "max_tokens": 50,
        "items": [{"x-api-key": "k1"}],
    }
    out = redact(event)
    assert out["headers"]["Authorization"] == MASK and out["headers"]["Accept"] == "json"
    assert out["llm_api_key"] == MASK and out["telegram_bot_token"] == MASK
    assert out["prompt_tokens"] == 12 and out["max_tokens"] == 50
    assert out["items"][0]["x-api-key"] == MASK


def test_json_logs_are_redacted_end_to_end() -> None:
    stream = io.StringIO()
    configure_logging("INFO", json_logs=True, stream=stream)
    register_secrets(["sk-live-key-0123456789"])
    structlog.get_logger("t").info(
        "llm_call", note="used sk-live-key-0123456789", authorization="Bearer abcdefgh123"
    )
    logging.getLogger("uvicorn.error").info("stdlib line with sk-live-key-0123456789")
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["event"] == "llm_call" and lines[0]["level"] == "info" and "timestamp" in lines[0]
    assert "sk-live-key-0123456789" not in stream.getvalue()
    assert "abcdefgh123" not in stream.getvalue()
    assert any("stdlib line" in line["event"] for line in lines)
