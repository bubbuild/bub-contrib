from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bub_qq.protocol.signature import _seed_from_secret
from bub_qq.protocol.signature import sign_validation_payload
from bub_qq.protocol.signature import verify_request_signature


def test_validation_signature_matches_official_example() -> None:
    signature = sign_validation_payload(
        secret="DG5g3B4j9X2KOErG",
        event_ts="1725442341",
        plain_token="Arq0D5A61EgUu4OxUvOp",
    )

    assert (
        signature
        == "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"
    )


def test_request_signature_accepts_valid_signature() -> None:
    secret = "naOC0ocQE3shWLAfffVLB1rhYPG7"
    timestamp = "1725442341"
    body = b'{ "op": 0,"d": {}, "t": "GATEWAY_EVENT_NAME"}'
    private_key = Ed25519PrivateKey.from_private_bytes(_seed_from_secret(secret))
    signature = private_key.sign(timestamp.encode("utf-8") + body).hex()

    assert verify_request_signature(
        secret=secret,
        timestamp=timestamp,
        body=body,
        signature_hex=signature,
    )


def test_empty_secret_is_rejected_instead_of_looping() -> None:
    import pytest

    from bub_qq.protocol.signature import derive_public_key

    with pytest.raises(ValueError, match="empty"):
        derive_public_key("")
