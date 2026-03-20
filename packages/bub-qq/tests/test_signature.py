from __future__ import annotations

from bub_qq.signature import sign_validation_payload
from bub_qq.signature import verify_request_signature


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


def test_request_signature_matches_official_example() -> None:
    assert verify_request_signature(
        secret="naOC0ocQE3shWLAfffVLB1rhYPG7",
        timestamp="1725442341",
        body=b'{ "op": 0,"d": {}, "t": "GATEWAY_EVENT_NAME"}',
        signature_hex="865ad13a61752ca65e26bde6676459cd36cf1be609375b37bd62af366e1dc25a8dc789ba7f14e017ada3d554c671a911bfdf075ba54835b23391d509579ed002",
    )
