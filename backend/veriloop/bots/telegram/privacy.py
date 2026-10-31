"""Telegram privacy controls (G6, D7, D-14).

* Chat ids are never stored: only HMAC-SHA256(chat id) with a key derived from the audit key (HKDF,
  purpose "telegram-chat-hmac"), so the same chat maps to the same opaque ref without being reversible.
* Raw message content lives only in `ephemeral_inputs` (expires_at = now + TELEGRAM_TTL_HOURS) and in
  the job's request/state until the same deadline (jobs.expires_at).
* `purge_expired` (every 10 min + at boot) deletes expired ephemeral rows and uploaded files, then
  redacts every job past its TTL: request_json and state_json are dropped, result_json keeps verdict
  metadata and tool evidence only (claim, URL, title, debate removed), media files are deleted and
  `redacted=true` is set. The audit chain never held the content (hashes only).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import shutil
from datetime import datetime
from pathlib import Path

import structlog

from veriloop.contracts import JobView
from veriloop.core.config import Settings
from veriloop.db import repo
from veriloop.deps import Deps
from veriloop.orchestrator.jobview import redact_view, view_json
from veriloop.services.audit import derive_secret

log = structlog.get_logger(__name__)

PURGE_INTERVAL_S = 600.0
HMAC_PURPOSE = "telegram-chat-hmac"


def chat_hmac(settings: Settings, chat_id: int | str) -> str:
    key = derive_secret(settings, HMAC_PURPOSE)
    return hmac.new(key, f"telegram-chat:{chat_id}".encode(), hashlib.sha256).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _delete_file(path_text: str, settings: Settings) -> bool:
    path = Path(path_text)
    if not _inside(path, settings.home):  # never delete outside VERILOOP_HOME, whatever the row says
        log.warning("ttl_purge_refused_path", path=path_text)
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("ttl_purge_file_failed", path=path_text, error=str(exc))
        return False
    return True


async def redact_job(deps: Deps, job_id: str) -> bool:
    row = await repo.get_job(deps.db, job_id)
    if row is None:
        return False
    result = None
    if row.result:
        result = view_json(redact_view(JobView.model_validate(row.result)))
    await repo.update_job(deps.db, job_id, request=None, state=None, result=result, redacted=True)
    media = deps.settings.home / "media" / job_id
    if media.is_dir() and _inside(media, deps.settings.home):
        await asyncio.to_thread(shutil.rmtree, media, True)
    log.info("job_redacted", job_id=job_id, reason="telegram_ttl")
    return True


async def purge_expired(deps: Deps, *, now: datetime | None = None) -> int:
    """Delete expired user content and redact jobs past their TTL. Returns rows purged + jobs redacted."""
    uploads = await deps.db.fetch(
        "SELECT content FROM ephemeral_inputs WHERE kind = 'upload_path' "
        "AND expires_at <= COALESCE($1::timestamptz, now())",
        now,
    )
    files = sum(_delete_file(r["content"], deps.settings) for r in uploads)
    deleted = await repo.purge_expired_ephemeral(deps.db, now=now)
    due = await repo.jobs_due_for_redaction(deps.db, now=now)
    redacted = 0
    for job_id in due:
        try:
            redacted += await redact_job(deps, job_id)
        except Exception as exc:  # one bad row must not stop the purge of the others
            log.error("job_redaction_failed", job_id=job_id, error=str(exc))
    if deleted or redacted or files:
        log.info("ttl_purge", ephemeral_deleted=deleted, files_deleted=files, jobs_redacted=redacted)
    return deleted + redacted


async def run_purge_loop(deps: Deps, interval_s: float = PURGE_INTERVAL_S) -> None:
    """Background task: purge at start, then every `interval_s` seconds. Never raises (except cancel)."""
    while True:
        try:
            await purge_expired(deps)
        except Exception as exc:
            log.error("ttl_purge_failed", error=str(exc))
        await asyncio.sleep(interval_s)
