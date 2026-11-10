from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from bub.channels.message import ChannelMessage

from bub_qq.outbound.group import QQGroupSendService
from bub_qq.outbound.media import MediaSpec
from bub_qq.outbound.media import outbound_context_for_media
from bub_qq.outbound.media import resolve_media_path
from bub_qq.outbound.upload import file_digests
from bub_qq.outbound.upload import upload_local_file
from bub_qq.session import QQSessionState


class MultipartStub:
    def __init__(self, *, block_size: int = 4) -> None:
        self.block_size = block_size
        self.puts: list[tuple[str, bytes]] = []
        self.finishes: list[dict[str, object]] = []
        self.merges: list[dict[str, object]] = []

    async def post_group_upload_prepare(
        self, *, group_openid: str, **body: object
    ) -> dict[str, object]:
        size = int(str(body["file_size"]))
        parts = []
        offset = 0
        index = 0
        while offset < size:
            chunk = min(self.block_size, size - offset)
            parts.append(
                {
                    "index": index,
                    "presigned_url": f"https://cos.example/{index}",
                    "block_size": str(chunk),
                }
            )
            offset += chunk
            index += 1
        return {
            "upload_id": "upload-1",
            "block_size": str(self.block_size),
            "parts": parts,
            "group_openid": group_openid,
        }

    async def post_c2c_upload_prepare(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("c2c prepare should not be used")

    async def post_group_upload_part_finish(
        self, *, group_openid: str, **body: object
    ) -> dict[str, object]:
        del group_openid
        self.finishes.append(body)
        return {}

    async def post_c2c_upload_part_finish(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("c2c finish should not be used")

    async def put_url(self, url: str, data: bytes) -> None:
        self.puts.append((url, data))

    async def post_group_file(self, **kwargs: object) -> dict[str, object]:
        self.merges.append(kwargs)
        return {"file_info": "MERGED"}

    async def post_c2c_file(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("c2c merge should not be used")

    async def post_group_media_message(self, **kwargs: object) -> dict[str, object]:
        self.merges.append({"op": "media", **kwargs})
        return {"id": "gmedia-local"}

    async def post_group_text_message(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("text should not be used")

    async def post_group_markdown_message(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("markdown should not be used")


def test_resolve_media_path_only_allows_outbox_and_inbox(tmp_path: Path) -> None:
    (tmp_path / "outbox").mkdir()
    (tmp_path / "inbox" / "msg-1").mkdir(parents=True)
    generated = tmp_path / "outbox" / "clip.mp4"
    generated.write_bytes(b"abcd")
    received = tmp_path / "inbox" / "msg-1" / "photo.png"
    received.write_bytes(b"png")
    (tmp_path / "notes.md").write_text("private", encoding="utf-8")
    (tmp_path / "outbox" / ".env").write_text("SECRET=1", encoding="utf-8")
    outside = tmp_path.parent / "outside.mp4"

    resolved, error = resolve_media_path("outbox/clip.mp4", str(tmp_path))
    assert error is None
    assert resolved == generated.resolve()
    resolved, error = resolve_media_path(str(received), str(tmp_path))
    assert error is None
    assert resolved == received.resolve()

    for raw in ("notes.md", str(outside), "outbox/.env"):
        resolved, error = resolve_media_path(raw, str(tmp_path))
        assert resolved is None
        assert error is not None and error.startswith("Not sent:")


def test_upload_local_file_puts_parts_then_merges(tmp_path: Path) -> None:
    async def _run() -> None:
        path = tmp_path / "clip.mp4"
        data = b"abcdefgh"
        path.write_bytes(data)
        client = MultipartStub(block_size=3)
        result = await upload_local_file(
            client,
            scope="group",
            openid="group-openid",
            path=path,
            file_type=2,
        )
        assert result["file_info"] == "MERGED"
        assert [chunk for _, chunk in client.puts] == [b"abc", b"def", b"gh"]
        assert len(client.finishes) == 3
        md5, sha1, md5_10m = file_digests(data)
        assert hashlib.md5(b"abc", usedforsecurity=False).hexdigest() == client.finishes[0]["md5"]
        assert client.merges[0]["upload_id"] == "upload-1"
        assert md5 and sha1 and md5_10m

    asyncio.run(_run())


def test_group_send_uploads_local_file(tmp_path: Path) -> None:
    async def _run() -> None:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"abcdefgh")
        state = QQSessionState()
        state.latest_message_id_by_session["qq:group:group-openid"] = "group-message-1"
        state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        openapi = MultipartStub(block_size=8)
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=state,
            openapi=openapi,
        )
        spec = MediaSpec(
            file_type=2, file_name="clip.mp4", local_path=str(path)
        )
        result = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="",
                channel="qq",
                context=outbound_context_for_media(spec),
            )
        )
        assert result == {"id": "gmedia-local"}
        assert openapi.merges[-1]["op"] == "media"
        assert openapi.merges[-1]["file_info"] == "MERGED"

    asyncio.run(_run())
