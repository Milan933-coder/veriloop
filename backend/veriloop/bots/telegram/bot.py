"""aiogram 3 adapter: polling or webhook, active only when TELEGRAM_BOT_TOKEN is set (D-15).

Updates are handled in background tasks so a webhook request (or the polling loop) returns at once
while the job runs. Webhook mode registers APP_BASE_URL/telegram/webhook with a secret token
(TELEGRAM_WEBHOOK_SECRET, or one derived from the audit key) that the API checks on every call.
Note: Telegram Bot API traffic goes through aiogram's own HTTP session, not ToolClient (there is no
registered tool for it); failures are logged here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

from veriloop.deps import Deps
from veriloop.services.audit import derive_secret
from veriloop.services.reporting import links

from .flow import IncomingMessage, SubmitFn, TelegramFlow, WaitFn

log = structlog.get_logger(__name__)

WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def webhook_secret(deps: Deps) -> str:
    configured = deps.settings.telegram_webhook_secret.get_secret_value().strip()
    return configured or derive_secret(deps.settings, "telegram-webhook").hex()


class _AiogramReplier:
    def __init__(self, message: Message) -> None:
        self.message = message

    async def reply(self, text: str) -> Any:
        return await self.message.reply(text)

    async def edit(self, handle: Any, text: str) -> None:
        try:
            await handle.edit_text(text, disable_web_page_preview=True)
        except Exception as exc:  # e.g. message deleted by the user: send a fresh one instead
            log.info("telegram_edit_failed", error=str(exc)[:200])
            await self.message.answer(text, disable_web_page_preview=True)

    async def send_photo(self, png: bytes, caption: str) -> None:
        await self.message.answer_photo(
            BufferedInputFile(png, filename="veriloop-report-card.png"), caption=caption
        )


def _incoming(message: Message, bot: Bot) -> IncomingMessage:
    text = message.text or message.caption
    media: Any = None
    kind: Any = None
    ext = ".bin"
    if message.photo:
        media, kind, ext = message.photo[-1], "photo", ".jpg"
    elif message.video:
        media, kind, ext = message.video, "video", ".mp4"
    elif message.document and (message.document.mime_type or "").startswith(("image/", "video/")):
        media, kind = message.document, "document"
        ext = Path(message.document.file_name or "").suffix.lower()[:6] or ".bin"
    download = None
    if media is not None:

        async def download(dest: Path, _media: Any = media) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            await bot.download(_media, destination=dest)

    return IncomingMessage(
        chat_id=message.chat.id, text=text, media_kind=kind, media_ext=ext, download=download
    )


class TelegramService:
    def __init__(self, deps: Deps, flow: TelegramFlow) -> None:
        self.deps = deps
        self.flow = flow
        self.mode = deps.settings.telegram_mode
        self.bot = Bot(token=deps.settings.telegram_bot_token.get_secret_value())
        self.dp = Dispatcher()
        self.secret = webhook_secret(deps)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._polling: asyncio.Task[Any] | None = None
        router = Router()
        router.message.register(self._on_help, CommandStart())
        router.message.register(self._on_help, Command("help"))
        router.message.register(self._on_message, F.text | F.caption | F.photo | F.video | F.document)
        self.dp.include_router(router)

    async def _on_help(self, message: Message) -> None:
        await message.answer(self.flow.help_text())

    async def _on_message(self, message: Message) -> None:
        incoming = _incoming(message, self.bot)
        task = asyncio.create_task(self.flow.handle(incoming, _AiogramReplier(message)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self) -> None:
        if self.mode == "webhook":
            url = f"{links.base(self.deps.settings)}{WEBHOOK_PATH}"
            await self.bot.set_webhook(url=url, secret_token=self.secret, allowed_updates=["message"])
            log.info("telegram_started", mode="webhook", url=url)
        else:
            await self.bot.delete_webhook(drop_pending_updates=False)
            self._polling = asyncio.create_task(
                self.dp.start_polling(self.bot, handle_signals=False, allowed_updates=["message"]),
                name="telegram-polling",
            )
            log.info("telegram_started", mode="polling")

    async def feed_webhook(self, update: dict[str, Any]) -> None:
        await self.dp.feed_webhook_update(self.bot, update)

    async def stop(self) -> None:
        if self._polling is not None:
            try:
                await self.dp.stop_polling()
            except RuntimeError:
                pass  # polling never started (e.g. network failure at boot)
            self._polling.cancel()
            await asyncio.gather(self._polling, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.bot.session.close()
        log.info("telegram_stopped")


async def start_telegram(deps: Deps, submit: SubmitFn, wait: WaitFn | None = None) -> TelegramService | None:
    """Start the bot when TELEGRAM_BOT_TOKEN is set; otherwise log "unconfigured" and return None."""
    if not deps.settings.is_set("telegram_bot_token"):
        log.info("telegram_unconfigured", reason="TELEGRAM_BOT_TOKEN is not set; the bot stays idle")
        return None
    if wait is None:
        raise ValueError("start_telegram needs a wait(job_id, timeout) callable to report results")
    service = TelegramService(deps, TelegramFlow(deps, submit, wait))
    try:
        await service.start()
    except Exception as exc:  # bad token / no network: keep the API up, report it
        log.error("telegram_start_failed", error=str(exc)[:300], mode=service.mode)
        await service.bot.session.close()
        return None
    return service
