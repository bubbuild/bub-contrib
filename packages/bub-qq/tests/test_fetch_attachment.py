from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import bub
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from bub.core.tools import ToolContext

from bub_qq import tools
from bub_qq.config import QQConfig
from bub_qq.inbound.c2c import QQC2CInboundService
from bub_qq.protocol.models import QQAttachment
from bub_qq.runtime import set_active_channel
from bub_qq.security import QQAccessPolicy
from bub_qq.session import QQInboundDeduper
from bub_qq.session import QQSessionState
from bub_qq.session import remember_session


class FakeChannel:
    name = "qq"

    def __init__(self, state: QQSessionState) -> None:
        self.session_state = state


@pytest.fixture
def server_url():
    async def photo(request: web.Request) -> web.Response:
        return web.Response(body=b"\x89PNG-bytes")

    app = web.Application()
    app.router.add_get("/photo.png", photo)
    ready = threading.Event()
    holder: dict = {}

    def serve() -> None:
        loop = asyncio.new_event_loop()
        server = TestServer(app, host="127.0.0.1")
        loop.run_until_complete(server.start_server())
        holder.update(loop=loop, server=server)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(5)
    yield str(holder["server"].make_url("/photo.png"))
    loop = holder["loop"]
    asyncio.run_coroutine_threadsafe(holder["server"].close(), loop).result(5)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)


def _attachment(url: str) -> QQAttachment:
    return QQAttachment(
        content_type="image/png",
        filename="photo.png",
        height=None,
        width=None,
        size=10,
        url=url,
        voice_wav_url=None,
        asr_refer_text=None,
    )


def _context(workspace: Path, session_id: str = "qq:c2c:u1") -> ToolContext:
    return ToolContext(
        tape=None,
        state={
            "qq": {"scope": "c2c", "sender_id": "u1", "session_id": session_id},
            "_runtime_workspace": str(workspace),
        },
    )


def _fetch(context: ToolContext, message_id: str = "m1", index: int = 0) -> str:
    return asyncio.run(
        tools.qq_fetch_attachment.run(message_id=message_id, index=index, context=context)
    )


@pytest.fixture
def state(monkeypatch):
    config = QQConfig.model_construct(download_allow_hosts="127.0.0.1")
    monkeypatch.setattr(bub, "ensure_config", lambda cls: config)
    value = QQSessionState()
    set_active_channel(FakeChannel(value))
    yield value
    set_active_channel(None)


def test_inbound_records_attachments_without_downloading(tmp_path: Path) -> None:
    state = QQSessionState()
    service = QQC2CInboundService(
        channel_name="qq",
        deduper=QQInboundDeduper(8),
        state=state,
        policy=QQAccessPolicy(),
    )
    service.parse_inbound(
        {
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "author": {"user_openid": "u1"},
                "content": "look",
                "id": "m1",
                "attachments": [{"url": "https://cdn.example/p.png", "filename": "p.png"}],
            },
        }
    )
    session_id, attachments = state.attachments_by_message_id["m1"]
    assert session_id == "qq:c2c:u1"
    assert attachments[0].url == "https://cdn.example/p.png"
    assert not (tmp_path / "inbox").exists()


def test_fetch_downloads_into_inbox_on_demand(state, tmp_path, server_url) -> None:
    remember_session(
        state,
        session_id="qq:c2c:u1",
        message_id="m1",
        timestamp=None,
        attachments=(_attachment(server_url),),
    )

    result = _fetch(_context(tmp_path))

    saved = tmp_path / "inbox" / "m1" / "photo.png"
    assert result == f"Downloaded to {saved.resolve()} (10 bytes)."
    assert saved.read_bytes() == b"\x89PNG-bytes"
    assert _fetch(_context(tmp_path)).startswith("Already downloaded")


def test_fetch_refuses_other_chats_and_bad_index(state, tmp_path, server_url) -> None:
    remember_session(
        state,
        session_id="qq:group:g1",
        message_id="m1",
        timestamp=None,
        attachments=(_attachment(server_url),),
    )
    assert "no attachments known" in _fetch(_context(tmp_path, "qq:c2c:u1"))
    group = _context(tmp_path, "qq:group:g1")
    assert "index must be between 0 and 0" in _fetch(group, index=3)
    assert not (tmp_path / "inbox").exists()


def test_fetch_is_guarded_against_private_addresses(tmp_path, server_url, monkeypatch) -> None:
    config = QQConfig.model_construct(download_allow_hosts="")
    monkeypatch.setattr(bub, "ensure_config", lambda cls: config)
    state = QQSessionState()
    set_active_channel(FakeChannel(state))
    try:
        remember_session(
            state,
            session_id="qq:c2c:u1",
            message_id="m1",
            timestamp=None,
            attachments=(_attachment(server_url),),
        )
        assert "not a public internet address" in _fetch(_context(tmp_path))
    finally:
        set_active_channel(None)
