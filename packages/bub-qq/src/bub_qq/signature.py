from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def _seed_from_secret(secret: str) -> bytes:
    seed = secret
    while len(seed) < 32:
        seed = seed * 2
    return seed[:32].encode("utf-8")


def derive_public_key(secret: str) -> Ed25519PublicKey:
    """Derive the public key from QQ bot secret using the documented seed rule."""

    private_key = Ed25519PrivateKey.from_private_bytes(_seed_from_secret(secret))
    return private_key.public_key()


def sign_validation_payload(*, secret: str, event_ts: str, plain_token: str) -> str:
    """Sign webhook validation payload as documented by QQ."""

    private_key = Ed25519PrivateKey.from_private_bytes(_seed_from_secret(secret))
    signature = private_key.sign(f"{event_ts}{plain_token}".encode("utf-8"))
    return signature.hex()


def verify_request_signature(
    *,
    secret: str,
    timestamp: str,
    body: bytes,
    signature_hex: str,
) -> bool:
    """Verify the QQ webhook request signature over timestamp + raw body."""

    if not timestamp or not signature_hex:
        return False

    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False

    if len(signature) != 64 or (signature[63] & 224) != 0:
        return False

    message = timestamp.encode("utf-8") + body
    public_key = derive_public_key(secret)
    try:
        public_key.verify(signature, message)
    except InvalidSignature:
        return False
    return True
