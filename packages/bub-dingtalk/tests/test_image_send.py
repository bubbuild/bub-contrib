"""Tests for outbound image extraction and dispatch in DingTalkChannel."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bub_dingtalk.channel import (
    DingTalkChannel,
    _extract_local_images,
    _mime_for_path,
)


# ---------------------------------------------------------------------------
# _extract_local_images
# ---------------------------------------------------------------------------


def test_extract_local_images_returns_path_for_existing_local_file(
    tmp_path: Path,
) -> None:
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfakepng")

    content = f"Here is the chart:\n![chart]({img})\nDone."
    stripped, images = _extract_local_images(content)

    assert stripped == "Here is the chart:\n\nDone."
    assert len(images) == 1
    assert images[0][0] == "chart"
    assert images[0][1] == img


def test_extract_local_images_keeps_http_urls() -> None:
    content = "See ![logo](https://example.com/logo.png) here."
    stripped, images = _extract_local_images(content)

    assert stripped == content
    assert images == []


def test_extract_local_images_keeps_data_urls() -> None:
    content = "![pic](data:image/png;base64,iVBORw0KGgo=)"
    stripped, images = _extract_local_images(content)

    assert stripped == content
    assert images == []


def test_extract_local_images_keeps_missing_local_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.png"
    content = f"![x]({missing})"
    stripped, images = _extract_local_images(content)

    assert stripped == content
    assert images == []


def test_extract_local_images_handles_file_scheme(tmp_path: Path) -> None:
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"fakejpg")
    content = f"![pic](file://{img})"
    stripped, images = _extract_local_images(content)

    assert stripped == ""
    assert len(images) == 1
    assert images[0][1] == img


def test_extract_local_images_multiple_in_order(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    content = f"first ![a]({a}) middle ![b]({b}) end"
    stripped, images = _extract_local_images(content)

    assert images[0][1] == a
    assert images[1][1] == b
    assert "first" in stripped and "middle" in stripped and "end" in stripped
    assert "![a]" not in stripped and "![b]" not in stripped


def test_mime_for_path_known_extensions() -> None:
    assert _mime_for_path(Path("x.png")) == "image/png"
    assert _mime_for_path(Path("x.jpg")) == "image/jpeg"
    assert _mime_for_path(Path("x.JPEG")) == "image/jpeg"
    assert _mime_for_path(Path("x.gif")) == "image/gif"
    assert _mime_for_path(Path("x.bmp")) == "image/bmp"


def test_mime_for_path_unknown_defaults_to_png() -> None:
    assert _mime_for_path(Path("x.webp")) == "image/png"


# ---------------------------------------------------------------------------
# DingTalkChannel.send — dispatch flow
# ---------------------------------------------------------------------------


def _build_channel(monkeypatch: pytest.MonkeyPatch) -> DingTalkChannel:
    """Construct a DingTalkChannel without running bub's full config loader."""
    fake_config = SimpleNamespace(
        client_id="cid",
        client_secret="csec",
        allow_users="*",
    )
    monkeypatch.setattr(
        "bub_dingtalk.channel.bub.ensure_config", lambda _cls: fake_config
    )

    async def _no_receive(_msg):  # pragma: no cover - never invoked here
        return None

    return DingTalkChannel(_no_receive)


def test_send_dispatches_text_then_image_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    img = tmp_path / "pic.png"
    img.write_bytes(b"fake-bytes")

    calls: list[tuple[str, tuple, dict]] = []

    def _fake_send_message(cid, secret, chat_id, content, *, title="Bub Reply"):
        calls.append(("text", (cid, secret, chat_id, content), {"title": title}))
        return {"ok": True}

    def _fake_upload_media(cid, secret, file_bytes, filename, mime_type):
        calls.append(
            (
                "upload",
                (cid, secret, len(file_bytes), filename, mime_type),
                {},
            )
        )
        return "MEDIA_ID_123"

    def _fake_send_image(cid, secret, chat_id, photo_ref):
        calls.append(("image", (cid, secret, chat_id, photo_ref), {}))
        return {"ok": True}

    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.send_message", _fake_send_message
    )
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.upload_media", _fake_upload_media
    )
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.send_image_message",
        _fake_send_image,
    )

    ch = _build_channel(monkeypatch)

    from bub.channels.message import ChannelMessage

    msg = ChannelMessage(
        session_id="dingtalk:user-1",
        content=f"see this:\n![pic]({img})",
        channel="dingtalk",
        chat_id="user-1",
    )

    asyncio.run(ch.send(msg))

    # Expect: text first, then upload, then image send
    assert [c[0] for c in calls] == ["text", "upload", "image"]

    text_call = calls[0]
    assert text_call[1][2] == "user-1"  # chat_id
    assert "see this:" in text_call[1][3]
    assert "![pic]" not in text_call[1][3]

    upload_call = calls[1]
    assert upload_call[1][3] == "pic.png"
    assert upload_call[1][4] == "image/png"

    image_call = calls[2]
    assert image_call[1][2] == "user-1"  # chat_id
    assert image_call[1][3] == "MEDIA_ID_123"


def test_send_image_only_when_text_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    img = tmp_path / "only.png"
    img.write_bytes(b"x")

    calls: list[str] = []
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.send_message",
        lambda *a, **k: calls.append("text") or {},
    )
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.upload_media",
        lambda *a, **k: calls.append("upload") or "MID",
    )
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.send_image_message",
        lambda *a, **k: calls.append("image") or {},
    )

    ch = _build_channel(monkeypatch)
    from bub.channels.message import ChannelMessage

    msg = ChannelMessage(
        session_id="dingtalk:user-1",
        content=f"![pic]({img})",
        channel="dingtalk",
        chat_id="user-1",
    )

    asyncio.run(ch.send(msg))

    assert calls == ["upload", "image"]


def test_send_skips_missing_file_and_just_sends_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    missing = tmp_path / "ghost.png"

    calls: list[str] = []
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.send_message",
        lambda *a, **k: calls.append("text") or {},
    )
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.upload_media",
        lambda *a, **k: calls.append("upload") or "MID",
    )
    monkeypatch.setattr(
        "skills.dingtalk.scripts.dingtalk_send.send_image_message",
        lambda *a, **k: calls.append("image") or {},
    )

    ch = _build_channel(monkeypatch)
    from bub.channels.message import ChannelMessage

    msg = ChannelMessage(
        session_id="dingtalk:user-1",
        content=f"missing: ![ghost]({missing})",
        channel="dingtalk",
        chat_id="user-1",
    )

    asyncio.run(ch.send(msg))

    assert calls == ["text"]  # no upload/image attempt
