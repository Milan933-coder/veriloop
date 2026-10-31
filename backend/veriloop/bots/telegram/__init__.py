"""Telegram bot (SPEC §2 ingestion, G6, D7, D-14): idle until TELEGRAM_BOT_TOKEN is set."""

from .bot import SECRET_HEADER, WEBHOOK_PATH, TelegramService, start_telegram, webhook_secret
from .flow import IncomingMessage, TelegramFlow, format_result, parse_message
from .privacy import chat_hmac, purge_expired, redact_job, run_purge_loop

__all__ = [
    "SECRET_HEADER",
    "WEBHOOK_PATH",
    "IncomingMessage",
    "TelegramFlow",
    "TelegramService",
    "chat_hmac",
    "format_result",
    "parse_message",
    "purge_expired",
    "redact_job",
    "run_purge_loop",
    "start_telegram",
    "webhook_secret",
]
