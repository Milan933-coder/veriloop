"""POST /verify body handling: JSON `VerifyRequest` or multipart (`file` + the same fields as form fields).

Uploads are copied to `settings.media_dir(job_id)/upload<ext>` (VERILOOP_HOME, never the repo or C:
temp; the real server also points Python's tempfile at VERILOOP_HOME/tmp so multipart spooling stays
there) with MAX_UPLOAD_MB enforced on the declared Content-Length and on the bytes actually copied.
"""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import IO, Any

import structlog
from fastapi import Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from veriloop.contracts import VerifyRequest
from veriloop.core.config import Settings
from veriloop.core.errors import ProblemError
from veriloop.core.executor import run_cpu

from .problems import validation_problem

log = structlog.get_logger(__name__)

MAX_JSON_BYTES = 256 * 1024
MAX_TEXT_CHARS = 20_000
MULTIPART_OVERHEAD = 1024 * 1024  # form fields + boundaries on top of the file itself
CLIENT_SOURCES = frozenset({"web", "pwa", "api", "extension"})  # "telegram" only via the bot
MEDIA_PREFIXES = ("image/", "video/", "audio/")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,5}$")
_FORM_FIELDS = ("url", "text", "platform", "locale", "source")


def _too_large(what: str, limit_bytes: int) -> ProblemError:
    return ProblemError(
        413,
        "Payload too large",
        f"{what} exceeds the limit of {limit_bytes // (1024 * 1024) or 1} MB.",
        type_="urn:veriloop:problem:too-large",
    )


def _declared_length(request: Request) -> int | None:
    try:
        return int(request.headers["content-length"])
    except (KeyError, ValueError):
        return None


def build_request(data: dict[str, Any], *, upload_id: str | None = None) -> VerifyRequest:
    """Validate client fields into a VerifyRequest (422 problem on any violation)."""
    if data.get("upload_id"):
        raise ProblemError(
            422,
            "Invalid request",
            "upload_id is set by the server; send the file as multipart.",
            type_="urn:veriloop:problem:validation",
        )
    source = data.get("source") or "web"
    if source not in CLIENT_SOURCES:
        raise ProblemError(
            422,
            "Invalid request",
            f"source must be one of {sorted(CLIENT_SOURCES)} (telegram jobs come from the bot).",
            type_="urn:veriloop:problem:validation",
        )
    url = data.get("url")
    if url is not None and not (
        isinstance(url, str) and re.match(r"^https?://\S+$", url.strip(), re.IGNORECASE)
    ):
        raise ProblemError(
            422,
            "Invalid request",
            "url must be an absolute http(s) URL.",
            type_="urn:veriloop:problem:validation",
        )
    text = data.get("text")
    if isinstance(text, str) and len(text) > MAX_TEXT_CHARS:
        raise ProblemError(
            422,
            "Invalid request",
            f"text is longer than {MAX_TEXT_CHARS} characters.",
            type_="urn:veriloop:problem:validation",
        )
    payload = {**data, "source": source, "upload_id": upload_id}
    if isinstance(url, str):
        payload["url"] = url.strip()
    if isinstance(text, str):
        payload["text"] = text.strip() or None
    payload = {k: v for k, v in payload.items() if v is not None or k == "upload_id"}
    try:
        return VerifyRequest.model_validate(payload)
    except ValidationError as exc:
        raise validation_problem(list(exc.errors()), "Invalid verification request") from exc


async def read_json_request(request: Request) -> VerifyRequest:
    declared = _declared_length(request)
    if declared is not None and declared > MAX_JSON_BYTES:
        raise _too_large("The JSON body", MAX_JSON_BYTES)
    body = await request.body()
    if len(body) > MAX_JSON_BYTES:
        raise _too_large("The JSON body", MAX_JSON_BYTES)
    try:
        data = json.loads(body or b"null")
    except ValueError as exc:
        raise ProblemError(
            400,
            "Malformed JSON",
            f"The request body is not valid JSON ({exc}).",
            type_="urn:veriloop:problem:malformed-json",
        ) from exc
    if not isinstance(data, dict):
        raise ProblemError(
            422,
            "Invalid request",
            "Send a JSON object with url, text or both.",
            type_="urn:veriloop:problem:validation",
        )
    return build_request(data)


def upload_extension(filename: str | None, content_type: str | None) -> str:
    ext = Path(filename or "").suffix.lower()
    if _EXT_RE.fullmatch(ext):
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ".bin"
    return guessed if _EXT_RE.fullmatch(guessed) else ".bin"


def _copy_limited(src: IO[bytes], dest: Path, limit: int) -> int:
    """Copy at most `limit` bytes; returns the size, or -1 (and removes dest) when the file is larger."""
    src.seek(0)
    size = 0
    with dest.open("wb") as out:
        while chunk := src.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                break
            out.write(chunk)
    if size > limit:
        dest.unlink(missing_ok=True)
        return -1
    return size


async def read_multipart(request: Request, settings: Settings, job_id: str) -> tuple[VerifyRequest, str]:
    """Save the uploaded file for `job_id` and build its VerifyRequest. Returns (request, upload_path)."""
    limit = settings.max_upload_mb * 1024 * 1024
    declared = _declared_length(request)
    if declared is not None and declared > limit + MULTIPART_OVERHEAD:
        raise _too_large("The upload", limit)
    form = await request.form(max_files=1, max_fields=len(_FORM_FIELDS) + 2)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ProblemError(
                422,
                "Invalid request",
                "multipart requests need a `file` part.",
                type_="urn:veriloop:problem:validation",
            )
        content_type = (upload.content_type or "").lower()
        ext = upload_extension(upload.filename, content_type)
        guessed_type = mimetypes.guess_type(f"x{ext}")[0] or ""
        if not (content_type.startswith(MEDIA_PREFIXES) or guessed_type.startswith(MEDIA_PREFIXES)):
            raise ProblemError(
                415,
                "Unsupported media type",
                f"Upload an image, video or audio file (got {content_type or 'unknown type'}).",
                type_="urn:veriloop:problem:unsupported-media",
            )
        dest = settings.media_dir(job_id) / f"upload{ext}"
        size = await run_cpu(_copy_limited, upload.file, dest, limit)
        if size < 0:
            raise _too_large("The upload", limit)
        if size == 0:
            dest.unlink(missing_ok=True)
            raise ProblemError(
                422, "Invalid request", "The uploaded file is empty.", type_="urn:veriloop:problem:validation"
            )
        fields = {name: form.get(name) for name in _FORM_FIELDS}
        data = {k: v for k, v in fields.items() if isinstance(v, str) and v != ""}
        data.setdefault("platform", "upload")
        verify = build_request(data, upload_id=job_id)
        log.info("upload_saved", job_id=job_id, bytes=size, content_type=content_type, ext=ext)
        return verify, str(dest)
    except ProblemError:
        shutil.rmtree(settings.home / "media" / job_id, ignore_errors=True)
        raise
    finally:
        await form.close()
