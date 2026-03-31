"""Ed25519 device identity for OpenClaw gateway handshake."""

from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

logger = logging.getLogger(__name__)

_DEFAULT_KEY_PATH = Path(__file__).resolve().parent.parent / ".dispatch_device_key"


def _generate_keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _save_key(private_key: Ed25519PrivateKey, path: Path) -> None:
    pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    path.write_bytes(pem)
    logger.info("Device key written to %s", path)


def _load_key(path: Path) -> Ed25519PrivateKey:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    return load_pem_private_key(path.read_bytes(), password=None)  # type: ignore[return-value]


def load_or_create_key(path: Path = _DEFAULT_KEY_PATH) -> Ed25519PrivateKey:
    """Load existing device key or generate a new one."""
    if path.exists():
        logger.info("Loading device key from %s", path)
        return _load_key(path)
    key = _generate_keypair()
    _save_key(key, path)
    return key


def device_fingerprint(private_key: Ed25519PrivateKey) -> str:
    """SHA-256 hex digest of the raw public key bytes."""
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding (matches OpenClaw JS client)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    """Base64url-encoded raw public key bytes."""
    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _b64url_encode(raw)


def sign_payload(private_key: Ed25519PrivateKey, payload: str) -> str:
    """Sign a UTF-8 payload string and return base64url signature."""
    signature = private_key.sign(payload.encode())
    return _b64url_encode(signature)
