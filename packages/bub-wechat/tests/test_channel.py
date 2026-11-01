import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import bub
from aiohttp import web
from bub.channels import ChannelMessage
from bub.channels.message import MediaItem
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from weixin_bot.api import MessageItemType

from bub_wechat import channel
from bub_wechat.channel import WeChatChannel, _parse_aes_key, get_token_path


def test_token_path_uses_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bub, "home", tmp_path)

    assert get_token_path() == tmp_path / "wechat_token.json"


def _encrypt(data: bytes, key: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padder.update(data) + padder.finalize()) + encryptor.finalize()


def test_parse_aes_key_accepts_hex_and_both_base64_forms() -> None:
    key = bytes(range(16))

    assert _parse_aes_key({"aeskey": key.hex(), "media": {"aes_key": ""}}) == key
    assert _parse_aes_key({"media": {"aes_key": base64.b64encode(key).decode()}}) == key
    hex_in_b64 = base64.b64encode(key.hex().encode()).decode()
    assert _parse_aes_key({"media": {"aes_key": hex_in_b64}}) == key
    assert _parse_aes_key({"media": {"aes_key": ""}}) is None


def test_download_image_decrypts_cdn_payload(monkeypatch) -> None:
    key = bytes(range(16))
    png = b"\x89PNG\r\n\x1a\n" + b"pixels" * 20
    queries: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        queries.append(request.query["encrypted_query_param"])
        return web.Response(body=_encrypt(png, key))

    async def run() -> MediaItem:
        app = web.Application()
        app.router.add_get("/c2c/download", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(channel, "CDN_BASE_URL", f"http://127.0.0.1:{port}/c2c")
        try:
            return await channel.download_image(
                {"media": {"encrypt_query_param": "a+b/c=", "aes_key": ""}, "aeskey": key.hex()}
            )
        finally:
            await runner.cleanup()

    media = asyncio.run(run())

    assert queries == ["a+b/c="]
    assert media.mime_type == "image/png"
    assert asyncio.run(media.data_fetcher()) == png


def test_failed_image_download_is_reported_in_content(monkeypatch) -> None:
    async def fail(image):
        raise RuntimeError("boom")

    monkeypatch.setattr(channel, "download_image", fail)
    item = {"type": MessageItemType.IMAGE, "image_item": {"media": {}}}

    text, media = asyncio.run(WeChatChannel._extract_media(item))

    assert text == "[图片下载失败]"
    assert media is None


def _channel_with_inbox(monkeypatch, hold_seconds: float) -> tuple[WeChatChannel, list[ChannelMessage]]:
    received: list[ChannelMessage] = []

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)

    async def fake_download(image):
        async def fetch() -> bytes:
            return b"img"

        return MediaItem(type="image", mime_type="image/png", data_fetcher=fetch)

    monkeypatch.setattr(channel, "download_image", fake_download)
    monkeypatch.setattr(channel, "IMAGE_HOLD_SECONDS", hold_seconds)
    wechat = WeChatChannel.__new__(WeChatChannel)
    wechat.on_receive = on_receive
    wechat._held = {}
    wechat.bot = SimpleNamespace(send_typing=None, stop_typing=None)
    return wechat, received


def _incoming(text: str, *items: dict) -> SimpleNamespace:
    return SimpleNamespace(user_id="u1", text=text, raw={"item_list": list(items)})


IMAGE_ITEM = {"type": MessageItemType.IMAGE, "image_item": {"media": {}}}


def _text_item(text: str) -> dict:
    return {"type": MessageItemType.TEXT, "text_item": {"text": text}}


def test_text_after_image_is_merged_into_one_message(monkeypatch) -> None:
    async def run() -> list[ChannelMessage]:
        wechat, received = _channel_with_inbox(monkeypatch, hold_seconds=5)
        await wechat.process_message(_incoming("[image]", IMAGE_ITEM))
        assert received == []
        await wechat.process_message(_incoming("这是什么", _text_item("这是什么")))
        return received

    received = asyncio.run(run())

    assert len(received) == 1
    assert received[0].content == "[image]\n这是什么"
    assert len(received[0].media) == 1


def test_lone_image_is_released_after_hold(monkeypatch) -> None:
    async def run() -> tuple[int, int]:
        wechat, received = _channel_with_inbox(monkeypatch, hold_seconds=0.05)
        await wechat.process_message(_incoming("[image]", IMAGE_ITEM))
        before = len(received)
        await asyncio.sleep(0.2)
        return before, len(received)

    assert asyncio.run(run()) == (0, 1)


def test_command_flushes_held_image_first(monkeypatch) -> None:
    async def run() -> list[ChannelMessage]:
        wechat, received = _channel_with_inbox(monkeypatch, hold_seconds=5)
        await wechat.process_message(_incoming("[image]", IMAGE_ITEM))
        await wechat.process_message(_incoming(",help", _text_item(",help")))
        return received

    received = asyncio.run(run())

    assert [m.kind for m in received] == ["normal", "command"]
    assert len(received[0].media) == 1
    assert received[1].media == []
