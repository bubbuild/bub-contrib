from __future__ import annotations

import asyncio
import base64
import json
import threading
from pathlib import Path

import bub
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from bub import configure
from bub.channels.message import ChannelMessage
from bub.core.tools import ToolContext, is_content_result

from bub_qq import tools
from bub_qq.config import QQConfig
from bub_qq.inbound.images import fetch_image, image_media, image_mime_type
from bub_qq.protocol.models import QQAttachment
from bub_qq.runtime import set_active_channel
from bub_qq.session import QQSessionState, remember_session

PNG = b"\x89PNG\r\n\x1a\n-fake-image"


@pytest.fixture
def server():
    async def photo(request: web.Request) -> web.Response:
        return web.Response(body=PNG, content_type="image/png")

    async def big(request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 4096, content_type="image/png")

    app = web.Application()
    app.router.add_get("/photo.png", photo)
    app.router.add_get("/big.png", big)
    ready = threading.Event()
    holder: dict = {}

    def serve() -> None:
        loop = asyncio.new_event_loop()
        test_server = TestServer(app, host="127.0.0.1")
        loop.run_until_complete(test_server.start_server())
        holder.update(loop=loop, server=test_server)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(5)
    yield holder["server"]
    loop = holder["loop"]
    asyncio.run_coroutine_threadsafe(holder["server"].close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def _attachment(url: str | None, content_type: str | None = "image/png", filename: str = "photo.png", size=None):
    return QQAttachment(
        content_type=content_type,
        filename=filename,
        height=None,
        width=None,
        size=size,
        url=url,
        voice_wav_url=None,
        asr_refer_text=None,
    )


@pytest.fixture
def allow_local(monkeypatch):
    config = QQConfig.model_construct(download_allow_hosts="127.0.0.1")
    monkeypatch.setattr(bub, "ensure_config", lambda cls: config)
    return config


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("image/png", "a.png", "image/png"),
        ("image/jpg", "a.jpg", "image/jpeg"),
        ("image/jpeg; charset=binary", "a", "image/jpeg"),
        (None, "a.webp", "image/webp"),
        ("", "a.gif", "image/gif"),
        ("image/bmp", "a.bmp", None),
        ("file", "report.pdf", None),
        (None, "notes.txt", None),
    ],
)
def test_image_mime_type(content_type, filename, expected) -> None:
    assert image_mime_type(_attachment("https://x/a", content_type, filename)) == expected


def test_image_media_picks_images_without_downloading(tmp_path) -> None:
    attachments = (
        _attachment("https://cdn.example/1.png"),
        _attachment("https://cdn.example/r.pdf", "file", "r.pdf"),
        _attachment("https://cdn.example/2.jpg", "image/jpeg", "2.jpg"),
        _attachment("https://cdn.example/huge.png", size=50),
        _attachment(None),
        _attachment("https://cdn.example/3.png"),
    )
    media, indices = image_media(attachments, workspace=tmp_path, message_id="m1", max_count=2, max_bytes=40)
    assert indices == [0, 2]
    assert [(item.type, item.mime_type) for item in media] == [("image", "image/png"), ("image", "image/jpeg")]
    assert not (tmp_path / "inbox").exists()


def test_fetch_image_downloads_once_into_inbox(allow_local, tmp_path, server) -> None:
    url = str(server.make_url("/photo.png"))
    data = asyncio.run(
        fetch_image(_attachment(url), workspace=tmp_path, message_id="m1", index=0, max_bytes=1024)
    )
    assert data == PNG
    assert (tmp_path / "inbox" / "m1" / "photo.png").read_bytes() == PNG
    # A second read uses the saved copy (the server is not needed for it).
    again = asyncio.run(
        fetch_image(_attachment("https://unreachable.invalid/x.png"), workspace=tmp_path, message_id="m1",
                    index=0, max_bytes=1024)
    )
    assert again == PNG


def test_fetch_image_respects_the_size_limit(allow_local, tmp_path, server) -> None:
    url = str(server.make_url("/big.png"))
    data = asyncio.run(
        fetch_image(_attachment(url, filename="big.png"), workspace=tmp_path, message_id="m1", index=0,
                    max_bytes=1024)
    )
    assert data is None


def test_fetch_image_is_guarded_against_private_addresses(monkeypatch, tmp_path, server) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: QQConfig.model_construct(download_allow_hosts=""))
    url = str(server.make_url("/photo.png"))
    data = asyncio.run(fetch_image(_attachment(url), workspace=tmp_path, message_id="m1", index=0, max_bytes=1024))
    assert data is None
    assert not (tmp_path / "inbox" / "m1" / "photo.png").exists()


def test_media_item_becomes_a_data_url(allow_local, tmp_path, server) -> None:
    url = str(server.make_url("/photo.png"))
    media, _ = image_media((_attachment(url),), workspace=tmp_path, message_id="m1", max_count=4, max_bytes=1024)
    data_url = asyncio.run(media[0].get_url())
    assert data_url == "data:image/png;base64," + base64.b64encode(PNG).decode()


# --- through the real channel -------------------------------------------------


@pytest.fixture
def channel_env(tmp_path, monkeypatch):
    from bub_qq.channel import QQChannel

    monkeypatch.chdir(tmp_path)

    def build(**qq):
        configure._config_data.clear()
        configure.merge(
            configure._config_data,
            {"qq": {"receive_mode": "webhook", "state_file": str(tmp_path / "state.json"), **qq}},
        )
        configure._global_config.clear()
        received: list[ChannelMessage] = []

        async def handler(message: ChannelMessage) -> None:
            received.append(message)

        return QQChannel(handler), received

    yield build
    set_active_channel(None)
    configure._global_config.clear()
    configure._config_data.clear()


def _c2c(message_id: str = "m1") -> dict:
    return {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "author": {"user_openid": "u1"},
            "content": "what is this?",
            "id": message_id,
            "timestamp": "2099-01-01T00:00:00+00:00",
            "attachments": [
                {"url": "https://cdn.example/p.png", "filename": "p.png", "content_type": "image/png"},
                {"url": "https://cdn.example/r.pdf", "filename": "r.pdf", "content_type": "file"},
            ],
        },
    }


def _group(mentioned: bool, message_id: str = "g1") -> dict:
    return {
        "op": 0,
        "t": "GROUP_AT_MESSAGE_CREATE" if mentioned else "GROUP_MESSAGE_CREATE",
        "d": {
            "author": {"member_openid": "m-1", "member_role": "member"},
            "content": "<@bot> look" if mentioned else "look",
            "id": message_id,
            "group_openid": "group-1",
            "timestamp": "2099-01-01T00:00:00+00:00",
            "mentions": [{"member_openid": "bot", "is_you": True}] if mentioned else [],
            "attachments": [{"url": "https://cdn.example/p.png", "filename": "p.png", "content_type": "image/png"}],
        },
    }


def _attached(message: ChannelMessage) -> list[bool]:
    return [bool(item.get("attached")) for item in json.loads(message.content)["attachments"]]


def test_private_message_images_are_attached(channel_env) -> None:
    channel, received = channel_env()
    asyncio.run(channel._handle_transport_payload(_c2c()))
    (message,) = received
    assert [(item.type, item.filename) for item in message.media] == [("image", "p.png")]
    assert _attached(message) == [True, False]


@pytest.mark.parametrize(
    ("mode", "mentioned", "attached"),
    [
        ("direct", True, True),
        ("direct", False, False),
        ("all", False, True),
        ("off", True, False),
    ],
)
def test_group_images_follow_inbound_images(channel_env, mode, mentioned, attached) -> None:
    channel, received = channel_env(inbound_images=mode, group_wake="all")
    asyncio.run(channel._handle_transport_payload(_group(mentioned)))
    (message,) = received
    assert bool(message.media) is attached
    assert _attached(message) == [attached]


# --- qq.view_image --------------------------------------------------------------


class FakeChannel:
    name = "qq"

    def __init__(self, state: QQSessionState) -> None:
        self.session_state = state


def _context(workspace: Path, session_id: str = "qq:group:group-1") -> ToolContext:
    return ToolContext(
        tape=None,
        state={"qq": {"scope": "group", "sender_id": "m-1", "session_id": session_id},
               "_runtime_workspace": str(workspace)},
    )


def _view(context: ToolContext, message_id: str = "g1", index: int = 0) -> dict:
    return asyncio.run(tools.qq_view_image.run(message_id=message_id, index=index, context=context))


def test_view_image_returns_the_image_to_the_model(allow_local, tmp_path, server) -> None:
    state = QQSessionState()
    set_active_channel(FakeChannel(state))
    try:
        remember_session(state, session_id="qq:group:group-1", message_id="g1", timestamp=None,
                         attachments=(_attachment(str(server.make_url("/photo.png"))),))
        result = _view(_context(tmp_path))
    finally:
        set_active_channel(None)
    assert is_content_result(result)
    text, image = result["content"]
    assert text["type"] == "text" and "photo.png" in text["text"]
    assert image["type"] == "image" and image["media_type"] == "image/png"
    assert base64.b64decode(image["data"]) == PNG


def test_view_image_reports_why_an_image_is_unavailable(monkeypatch, tmp_path, server) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: QQConfig.model_construct(
        download_allow_hosts="", inbound_image_max_bytes=1024))
    state = QQSessionState()
    set_active_channel(FakeChannel(state))
    try:
        remember_session(state, session_id="qq:group:group-1", message_id="g1", timestamp=None,
                         attachments=(_attachment(str(server.make_url("/photo.png"))),
                                      _attachment("https://cdn.example/huge.png", size=4096)))
        blocked = _view(_context(tmp_path))
        too_large = _view(_context(tmp_path), index=1)
    finally:
        set_active_channel(None)
    assert blocked["content"][0]["text"].startswith("Not shown: Private network address check failed, access refused. ")
    assert "do not fetch the URL another way" in blocked["content"][0]["text"]
    # Only refused addresses carry the guidance.
    assert too_large["content"][0]["text"] == "Not shown: the image is larger than the 1024-byte limit"


def test_view_image_refuses_other_chats_and_non_images(allow_local, tmp_path) -> None:
    state = QQSessionState()
    set_active_channel(FakeChannel(state))
    try:
        remember_session(state, session_id="qq:group:group-1", message_id="g1", timestamp=None,
                         attachments=(_attachment("https://cdn.example/r.pdf", "file", "r.pdf"),))
        other_chat = _view(_context(tmp_path, "qq:group:other"))
        not_image = _view(_context(tmp_path))
    finally:
        set_active_channel(None)
    assert "no attachments known" in other_chat["content"][0]["text"]
    assert "not a PNG, JPEG, GIF or WebP" in not_image["content"][0]["text"]


def test_view_image_is_on_the_allowlist() -> None:
    from bub.hooks.interception import ToolCall

    from bub_qq.guard import classify

    assert classify(ToolCall(run_id="r", tool="qq.view_image", arguments={})) == "safe"


# --- raw payload log ------------------------------------------------------------


def test_raw_payload_is_logged_only_at_debug(channel_env) -> None:
    from loguru import logger

    channel, _ = channel_env()
    info: list[str] = []
    debug: list[str] = []
    sinks = [
        logger.add(lambda m: info.append(str(m)), level="INFO"),
        logger.add(lambda m: debug.append(str(m)), level="DEBUG"),
    ]
    try:
        asyncio.run(channel._handle_transport_payload(_c2c()))
    finally:
        for sink in sinks:
            logger.remove(sink)
    (line,) = [m for m in debug if "qq.transport.payload" in m]
    assert "t=C2C_MESSAGE_CREATE" in line and '"content": "what is this?"' in line
    assert not any("qq.transport.payload" in m for m in info)


# --- quoted images ----------------------------------------------------------------


def _quote(*, mentioned: bool, own: list[dict] | None = None, quoted_url: str = "https://cdn.example/q.png",
           message_id: str = "q1", scope: str = "group") -> dict:
    """A quote event shaped like QQ's: the quoted message's attachments in msg_elements."""

    quoted = {
        "message_type": 103,
        "content": "",
        "msg_idx": "REFIDX_abc",
        "attachments": [{"url": quoted_url, "filename": "q.png", "content_type": "image/png"}],
    }
    if scope == "c2c":
        event = _c2c(message_id)
        event["d"]["attachments"] = own or []
    else:
        event = _group(mentioned, message_id)
        event["d"]["attachments"] = own or []
        event["d"]["content"] = "<@bot> 这是什么" if mentioned else "这是什么"
    event["d"]["message_type"] = 103
    event["d"]["msg_elements"] = [quoted]
    return event


def test_quoted_image_is_listed_and_viewable_through_the_quoting_message(channel_env) -> None:
    channel, received = channel_env(group_wake="all")
    asyncio.run(channel._handle_transport_payload(_quote(mentioned=False)))
    (message,) = received
    payload = json.loads(message.content)
    (quoted,) = payload["quoted_messages"]
    (entry,) = quoted["attachments"]
    assert {k: entry[k] for k in ("index", "content_type", "filename", "url")} == {
        "index": 0, "content_type": "image/png", "filename": "q.png", "url": "https://cdn.example/q.png"
    }
    assert "attached" not in entry
    # Not addressed to the bot: listed, not attached; qq.view_image can reach it.
    assert not message.media
    session_id, attachments = channel.session_state.attachments_by_message_id["q1"]
    assert session_id == "qq:group:group-1"
    assert [a.filename for a in attachments] == ["q.png"]


def test_quoted_image_is_attached_when_addressed_to_the_bot(channel_env) -> None:
    channel, received = channel_env(group_wake="all")
    asyncio.run(channel._handle_transport_payload(_quote(mentioned=True)))
    (message,) = received
    assert [(item.type, item.filename) for item in message.media] == [("image", "q.png")]
    assert json.loads(message.content)["quoted_messages"][0]["attachments"][0]["attached"] is True


def test_quoted_attachments_are_numbered_after_the_messages_own(channel_env) -> None:
    channel, received = channel_env()
    own = [
        {"url": "https://cdn.example/p.png", "filename": "p.png", "content_type": "image/png"},
        {"url": "https://cdn.example/r.pdf", "filename": "r.pdf", "content_type": "file"},
    ]
    asyncio.run(channel._handle_transport_payload(_quote(mentioned=True, own=own, scope="c2c")))
    (message,) = received
    payload = json.loads(message.content)
    assert [a.get("attached", False) for a in payload["attachments"]] == [True, False]
    assert payload["quoted_messages"][0]["attachments"][0]["index"] == 2
    assert payload["quoted_messages"][0]["attachments"][0]["attached"] is True
    assert [item.filename for item in message.media] == ["p.png", "q.png"]
    _, attachments = channel.session_state.attachments_by_message_id["q1"]
    assert [a.filename for a in attachments] == ["p.png", "r.pdf", "q.png"]


def test_view_image_opens_a_quoted_image(allow_local, channel_env, tmp_path, server) -> None:
    channel, _ = channel_env(group_wake="all")
    url = str(server.make_url("/photo.png"))
    asyncio.run(channel._handle_transport_payload(_quote(mentioned=False, quoted_url=url)))
    set_active_channel(channel)
    result = _view(_context(tmp_path), message_id="q1", index=0)
    assert is_content_result(result)
    assert base64.b64decode(result["content"][1]["data"]) == PNG
