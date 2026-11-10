from __future__ import annotations

import asyncio
from pathlib import Path

from bub.channels.message import ChannelMessage

from bub_qq.outbound.c2c import QQC2CSendService
from bub_qq.outbound.group import QQGroupSendService
from bub_qq.outbound.media import FILE_TYPE_FILE
from bub_qq.outbound.media import FILE_TYPE_IMAGE
from bub_qq.outbound.media import FILE_TYPE_VIDEO
from bub_qq.outbound.media import FILE_TYPE_VOICE
from bub_qq.outbound.media import infer_file_type
from bub_qq.outbound.media import media_spec_from_args
from bub_qq.outbound.media import outbound_context_for_media
from bub_qq.session import QQSessionState


class GroupMediaOpenAPI:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_group_text_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "text", **kwargs})
        return {"id": "text"}

    async def post_group_markdown_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "markdown", **kwargs})
        return {"id": "markdown"}

    async def post_group_active_text_message(
        self, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append({"op": "active", **kwargs})
        return {"id": "active"}

    async def post_group_file(
        self,
        *,
        group_openid: str,
        file_type: int,
        url: str,
        file_name: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "op": "file",
                "group_openid": group_openid,
                "file_type": file_type,
                "url": url,
                "file_name": file_name,
            }
        )
        return {"file_info": "GFILE"}

    async def post_group_media_message(
        self,
        *,
        group_openid: str,
        file_info: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "op": "media",
                "group_openid": group_openid,
                "file_info": file_info,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "gmedia-1"}


class MultipartMediaOpenAPI:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_c2c_text_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "text", **kwargs})
        return {"id": "text"}

    async def post_c2c_markdown_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "markdown", **kwargs})
        return {"id": "markdown"}

    async def post_group_text_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "text", **kwargs})
        return {"id": "text"}

    async def post_group_markdown_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "markdown", **kwargs})
        return {"id": "markdown"}

    async def post_group_active_text_message(
        self, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append({"op": "active", **kwargs})
        return {"id": "active"}

    def _prepare(self, size: int) -> dict[str, object]:
        return {
            "upload_id": "upload-1",
            "parts": [
                {
                    "index": 0,
                    "presigned_url": "https://cos.example/0",
                    "block_size": str(size),
                }
            ],
        }

    async def post_c2c_upload_prepare(
        self, *, openid: str, **body: object
    ) -> dict[str, object]:
        self.calls.append({"op": "prepare", "scope": "c2c", "openid": openid, **body})
        return self._prepare(int(str(body["file_size"])))

    async def post_group_upload_prepare(
        self, *, group_openid: str, **body: object
    ) -> dict[str, object]:
        self.calls.append(
            {"op": "prepare", "scope": "group", "group_openid": group_openid, **body}
        )
        return self._prepare(int(str(body["file_size"])))

    async def post_c2c_upload_part_finish(self, **body: object) -> dict[str, object]:
        self.calls.append({"op": "part_finish", "scope": "c2c", **body})
        return {}

    async def post_group_upload_part_finish(self, **body: object) -> dict[str, object]:
        self.calls.append({"op": "part_finish", "scope": "group", **body})
        return {}

    async def put_url(self, url: str, data: bytes) -> None:
        self.calls.append({"op": "put", "url": url, "n": len(data)})

    async def post_c2c_file(self, **kwargs: object) -> dict[str, object]:
        if kwargs.get("url"):
            raise AssertionError("url upload must not be used")
        self.calls.append({"op": "merge", "scope": "c2c", **kwargs})
        return {"file_info": "FILEINFO"}

    async def post_group_file(self, **kwargs: object) -> dict[str, object]:
        if kwargs.get("url"):
            raise AssertionError("url upload must not be used")
        self.calls.append({"op": "merge", "scope": "group", **kwargs})
        return {"file_info": "GFILE"}

    async def post_c2c_media_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "media", **kwargs})
        return {"id": "media-1"}

    async def post_group_media_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append({"op": "media", **kwargs})
        return {"id": "gmedia-1"}


def _write_download(tmp_path: Path, payload: bytes = b"png-bytes"):
    async def download(
        url: str, *, workspace: Path, file_name: str | None = None
    ) -> Path:
        dest = tmp_path / (file_name or "pic.png")
        dest.write_bytes(payload)
        return dest

    return download


def test_infer_file_type_from_url_suffix() -> None:
    assert infer_file_type("https://cdn.example/a.PNG") == FILE_TYPE_IMAGE
    assert infer_file_type("https://cdn.example/a.jpg?x=1") == FILE_TYPE_IMAGE
    assert infer_file_type("https://cdn.example/a.mp4") == FILE_TYPE_VIDEO
    assert infer_file_type("https://cdn.example/a.silk") == FILE_TYPE_VOICE
    assert infer_file_type("https://cdn.example/a.pdf") == FILE_TYPE_FILE
    assert infer_file_type("https://cdn.example/noext") == FILE_TYPE_FILE


def test_media_spec_from_args_validates_url_and_type() -> None:
    spec, error = media_spec_from_args("https://example.com/pic.png", None)
    assert error is None
    assert spec is not None
    assert spec.file_type == FILE_TYPE_IMAGE
    assert spec.file_name == "pic.png"

    spec, error = media_spec_from_args("ftp://example.com/pic.png", None)
    assert spec is None
    assert error is not None

    spec, error = media_spec_from_args("https://example.com/pic.png", 9)
    assert spec is None
    assert error is not None

    spec, error = media_spec_from_args(None, None)
    assert spec is None
    assert error is None


def test_c2c_send_downloads_media_url_then_uploads(tmp_path: Path) -> None:
    async def _run() -> None:
        state = QQSessionState()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        spec, error = media_spec_from_args("https://example.com/pic.png", None)
        assert error is None and spec is not None
        openapi = MultipartMediaOpenAPI()
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
            workspace=tmp_path,
            download_media=_write_download(tmp_path),
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="",
                channel="qq",
                context=outbound_context_for_media(spec),
            )
        )

        assert result == {"id": "media-1"}
        assert [call["op"] for call in openapi.calls] == [
            "prepare",
            "put",
            "part_finish",
            "merge",
            "media",
        ]
        assert openapi.calls[3].get("url") in {None, ""}
        assert openapi.calls[4]["file_info"] == "FILEINFO"

    asyncio.run(_run())


def test_group_send_downloads_media_url_then_uploads(tmp_path: Path) -> None:
    async def _run() -> None:
        state = QQSessionState()
        state.latest_message_id_by_session["qq:group:group-openid"] = "group-message-1"
        state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        spec, error = media_spec_from_args("https://example.com/clip.mp4", 2)
        assert error is None and spec is not None
        openapi = MultipartMediaOpenAPI()
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=state,
            openapi=openapi,
            active_messages=True,
            workspace=tmp_path,
            download_media=_write_download(tmp_path, b"video"),
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="ignored caption",
                channel="qq",
                context=outbound_context_for_media(spec),
            )
        )

        assert result == {"id": "gmedia-1"}
        assert [call["op"] for call in openapi.calls] == [
            "prepare",
            "put",
            "part_finish",
            "merge",
            "media",
        ]
        assert openapi.calls[0]["file_type"] == 2
        assert openapi.calls[3].get("url") in {None, ""}
        assert openapi.calls[4]["file_info"] == "GFILE"

    asyncio.run(_run())


def test_group_media_does_not_use_active_fallback() -> None:
    async def _run() -> None:
        spec, error = media_spec_from_args("https://example.com/pic.png", 1)
        assert error is None and spec is not None
        openapi = GroupMediaOpenAPI()
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=QQSessionState(),
            openapi=openapi,
            active_messages=True,
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

        assert result is None
        assert openapi.calls == []

    asyncio.run(_run())


def test_group_media_download_failure_does_not_consume_msg_seq(
    tmp_path: Path,
) -> None:
    async def download(
        url: str, *, workspace: Path, file_name: str | None = None
    ) -> Path:
        raise ValueError("connection reset")

    async def _run() -> None:
        state = QQSessionState()
        state.latest_message_id_by_session["qq:group:group-openid"] = "group-message-1"
        state.latest_timestamp_by_session["qq:group:group-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        spec, error = media_spec_from_args("https://i.imgur.com/pic.png", 1)
        assert error is None and spec is not None
        openapi = MultipartMediaOpenAPI()
        service = QQGroupSendService(
            channel_name="qq",
            receive_mode="websocket",
            state=state,
            openapi=openapi,
            workspace=tmp_path,
            download_media=download,
        )

        failed = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="",
                channel="qq",
                context=outbound_context_for_media(spec),
            )
        )
        text = await service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                chat_id="group:group-openid",
                content="fallback text",
                channel="qq",
            )
        )

        assert failed is not None
        assert failed["status"] == "failed"
        assert "failed to download media_url" in str(failed["error"])
        assert text == {"id": "text"}
        assert [call["op"] for call in openapi.calls] == ["text"]
        assert openapi.calls[0]["msg_seq"] == 1

    asyncio.run(_run())
