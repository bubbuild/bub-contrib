import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from bub_http_bridge.channel import HttpBridgeChannel, HttpBridgeSettings


@pytest.fixture
def mock_handler():
    return AsyncMock()


@pytest.fixture
def channel(mock_handler):
    ch = HttpBridgeChannel(on_receive=mock_handler)
    return ch


@pytest.mark.asyncio
async def test_post_message_success(channel, mock_handler):
    """Test posting a valid message returns 202 and calls handler."""
    async with TestClient(TestServer(channel._app)) as client:
        resp = await client.post(
            "/message",
            json={"session_id": "telegram:12345", "content": ",echo hello", "source": "codex"},
        )
        assert resp.status == 202
        data = await resp.json()
        assert data["status"] == "accepted"

        mock_handler.assert_called_once()
        msg = mock_handler.call_args[0][0]
        assert msg.session_id == "telegram:12345"
        assert msg.content == ",echo hello"
        assert msg.channel == "http-bridge"
        assert msg.output_channel == "telegram"
        assert msg.chat_id == "12345"
        assert msg.context["source"] == "codex"


@pytest.mark.asyncio
async def test_post_message_missing_fields(channel, mock_handler):
    """Test posting without required fields returns 400."""
    async with TestClient(TestServer(channel._app)) as client:
        # Missing content
        resp = await client.post(
            "/message",
            json={"session_id": "telegram:12345"},
        )
        assert resp.status == 400

        # Missing session_id
        resp = await client.post(
            "/message",
            json={"content": "hello"},
        )
        assert resp.status == 400

        mock_handler.assert_not_called()


@pytest.mark.asyncio
async def test_post_message_invalid_json(channel, mock_handler):
    """Test posting invalid JSON returns 400."""
    async with TestClient(TestServer(channel._app)) as client:
        resp = await client.post(
            "/message",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        mock_handler.assert_not_called()


@pytest.mark.asyncio
async def test_session_id_without_colon(channel, mock_handler):
    """Test session_id without colon uses http-bridge as output channel."""
    async with TestClient(TestServer(channel._app)) as client:
        resp = await client.post(
            "/message",
            json={"session_id": "mysession", "content": "hello"},
        )
        assert resp.status == 202

        msg = mock_handler.call_args[0][0]
        assert msg.channel == "http-bridge"
        assert msg.output_channel == "http-bridge"
        assert msg.chat_id == "mysession"
        assert msg.session_id == "mysession"
        assert msg.context["source"] == "unknown"