"""Image helpers shared by the media services: hashing, decoding, crops and data URLs.

All functions are synchronous and CPU-bound; callers run them through `run_cpu`.
pHash is imagehash's 64-bit DCT hash (16 hex chars), the format `media_assets.phash` stores.
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import imagehash
import numpy as np
import scipy.fftpack  # noqa: F401  (imagehash imports it lazily on the first hash: ~1 s; pay it at import)
from PIL import Image, ImageOps, UnidentifiedImageError

# Frames this flat (fades, black/white slates, letterbox) hash alike across unrelated videos, so
# provenance never searches with them: a black frame would "match" every other black frame.
_MIN_GRAY_STD = 12.0

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif"}


@dataclass(frozen=True)
class ImageInfo:
    path: str
    sha256: str
    phash: str
    width: int
    height: int
    informative: bool
    format: str | None


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def load_rgb(path: str | Path) -> Image.Image:
    """Decode, apply the EXIF orientation (phone photos) and convert to RGB. Raises ValueError."""
    try:
        with Image.open(path) as im:
            im.seek(0)  # first frame of GIF/WebP animations
            img = ImageOps.exif_transpose(im)
            return img.convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError(f"not a decodable image: {Path(path).name}: {exc}") from exc


def is_image_file(path: str | Path) -> bool:
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def phash_hex(img: Image.Image) -> str:
    return str(imagehash.phash(img, hash_size=8))


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_informative(img: Image.Image) -> bool:
    gray = np.asarray(img.convert("L").resize((64, 64)), dtype=np.float32)
    return float(gray.std()) >= _MIN_GRAY_STD


def describe_image(path: str | Path) -> ImageInfo:
    img = load_rgb(path)
    with Image.open(path) as raw:
        fmt = raw.format
    return ImageInfo(
        path=str(path),
        sha256=sha256_file(path),
        phash=phash_hex(img),
        width=img.width,
        height=img.height,
        informative=is_informative(img),
        format=fmt,
    )


def center_crop(img: Image.Image, frac: float = 0.7) -> Image.Image:
    """Middle `frac` of each side: drops the verdict stamps/borders fact-checkers add to thumbnails."""
    w, h = img.size
    cw, ch = max(1, int(w * frac)), max(1, int(h * frac))
    left, top = (w - cw) // 2, (h - ch) // 2
    return img.crop((left, top, left + cw, top + ch))


def resized(img: Image.Image, max_side: int) -> Image.Image:
    if max(img.size) <= max_side:
        return img
    out = img.copy()
    out.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    return out


def to_jpeg_bytes(img: Image.Image, *, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def to_data_url(path: str | Path, *, max_side: int = 768, quality: int = 85) -> str:
    """JPEG data URL for the LLM's image input (downscaled: fewer image tokens, same scene)."""
    img = resized(load_rgb(path), max_side)
    return "data:image/jpeg;base64," + base64.b64encode(to_jpeg_bytes(img, quality=quality)).decode("ascii")


def save_jpeg(img: Image.Image, path: str | Path, *, quality: int = 95) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, format="JPEG", quality=quality)
    return path
