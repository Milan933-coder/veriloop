"""Ed25519 audit signing key (D-08) and secrets derived from it.

Source of the private key, in priority order:
  1. `AUDIT_ED25519_KEY` (PKCS#8 PEM; literal "\\n" sequences from a one-line .env value are accepted)
  2. `VERILOOP_HOME/keys/audit_ed25519.pem`, generated on first use (exclusive create, so two
     processes booting at once cannot end up with different keys).

The public key is published as PEM SubjectPublicKeyInfo at GET /audit/pubkey; the fingerprint is
sha256 of that DER encoding (reproducible with `openssl pkey -pubin -outform DER | sha256sum`).
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from veriloop.core.config import Settings

log = structlog.get_logger(__name__)

KEY_FILE_NAME = "audit_ed25519.pem"
ALGORITHM = "Ed25519"

_cache: dict[str, SigningKey] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class SigningKey:
    private: Ed25519PrivateKey
    public_pem: str
    fingerprint: str
    source: str  # env | file | generated

    @property
    def public(self) -> Ed25519PublicKey:
        return self.private.public_key()

    def sign(self, message: str) -> str:
        """Base64 (standard alphabet, padded) Ed25519 signature over the UTF-8 message."""
        return base64.b64encode(self.private.sign(message.encode("utf-8"))).decode("ascii")

    def verify(self, message: str, signature_b64: str) -> bool:
        return verify_signature(self.public, message, signature_b64)


def verify_signature(public: Ed25519PublicKey, message: str, signature_b64: str) -> bool:
    try:
        public.verify(base64.b64decode(signature_b64, validate=True), message.encode("utf-8"))
    except (InvalidSignature, ValueError):
        return False
    return True


def public_pem_and_fingerprint(public: Ed25519PublicKey) -> tuple[str, str]:
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return pem.decode("ascii"), hashlib.sha256(der).hexdigest()


def _from_private(private: Ed25519PrivateKey, source: str) -> SigningKey:
    pem, fingerprint = public_pem_and_fingerprint(private.public_key())
    return SigningKey(private=private, public_pem=pem, fingerprint=fingerprint, source=source)


def _parse_private_pem(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"audit key must be Ed25519, got {type(key).__name__}")
    return key


def _private_pem(private: Ed25519PrivateKey) -> bytes:
    return private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def generate_private_pem() -> str:
    """A fresh PKCS#8 PEM (used by tests and by operators who want to set AUDIT_ED25519_KEY)."""
    return _private_pem(Ed25519PrivateKey.generate()).decode("ascii")


def key_path(settings: Settings) -> Path:
    return settings.keys_dir / KEY_FILE_NAME


def _load_or_create_file(path: Path) -> tuple[Ed25519PrivateKey, str]:
    if path.is_file():
        return _parse_private_pem(path.read_text(encoding="utf-8")), "file"
    path.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    try:
        # O_EXCL: if another process won the race, use its key instead of overwriting it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _parse_private_pem(path.read_text(encoding="utf-8")), "file"
    with os.fdopen(fd, "wb") as fh:
        fh.write(_private_pem(private))
    log.warning("audit_key_generated", path=str(path), note="back this file up; it signs the audit chain")
    return private, "generated"


def load_signing_key(settings: Settings) -> SigningKey:
    """The process-wide signing key for these settings (cached; safe to call on every append)."""
    pem = settings.audit_ed25519_key.get_secret_value().strip().replace("\\n", "\n")
    cache_key = (
        "env:" + hashlib.sha256(pem.encode()).hexdigest() if pem else "file:" + str(key_path(settings))
    )
    with _lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached
        if pem:
            key = _from_private(_parse_private_pem(pem), "env")
        else:
            private, source = _load_or_create_file(key_path(settings))
            key = _from_private(private, source)
        _cache[cache_key] = key
    log.info("audit_key_loaded", source=key.source, fingerprint=key.fingerprint)
    return key


def public_key_pem(settings: Settings) -> str:
    """PEM SubjectPublicKeyInfo of the audit key (GET /audit/pubkey)."""
    return load_signing_key(settings).public_pem


def derive_secret(settings: Settings, purpose: str, length: int = 32) -> bytes:
    """A purpose-bound secret derived from the audit key (HKDF-SHA256), e.g. the chat-id HMAC key.

    Deriving instead of reusing the signing key keeps the two uses cryptographically separate while
    needing no extra secret to configure.
    """
    private = load_signing_key(settings).private
    raw = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=None, info=f"veriloop:{purpose}".encode()
    ).derive(raw)
