from __future__ import annotations

import asyncio

from bub.channels.message import ChannelMessage

from bub_qq.outbound.c2c import QQC2CSendService
from bub_qq.session import QQSessionState
from bub_qq.outbound.markdown import looks_like_markdown
from bub_qq.outbound.markdown import send_with_markdown_fallback
from bub_qq.protocol.errors import QQKnownOpenAPIError
from bub_qq.protocol.errors import QQOpenAPIError


class RecordingSender:
    def __init__(self, *, name: str, error: QQOpenAPIError | None = None) -> None:
        self.name = name
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "name": self.name,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        if self.error is not None:
            raise self.error
        return {"id": self.name}


class OpenAPIStub:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "openid": openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
            }
        )
        return {"id": "reply-1"}

    async def post_c2c_markdown_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "openid": openid,
                "content": content,
                "msg_id": msg_id,
                "msg_seq": msg_seq,
                "msg_type": 2,
            }
        )
        return {"id": "reply-1"}


class MarkdownFallbackOpenAPIStub:
    def __init__(self, error: QQOpenAPIError) -> None:
        self.error = error
        self.calls: list[str] = []

    async def post_c2c_text_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        del openid, content, msg_id, msg_seq
        self.calls.append("text")
        return {"id": "reply-text"}

    async def post_c2c_markdown_message(
        self,
        *,
        openid: str,
        content: str,
        msg_id: str,
        msg_seq: int,
    ) -> dict[str, object]:
        del openid, content, msg_id, msg_seq
        self.calls.append("markdown")
        raise self.error


def test_looks_like_markdown_detects_common_qq_syntax() -> None:
    assert looks_like_markdown("**加粗**")
    assert looks_like_markdown("see *this* now")
    assert looks_like_markdown("# 标题\n内容")
    assert looks_like_markdown("- 列表项")
    assert looks_like_markdown("1. 有序")
    assert looks_like_markdown("> 引用")
    assert looks_like_markdown("[链接](https://example.com)")
    assert looks_like_markdown("~~删除~~")
    assert looks_like_markdown("_斜体_")


def test_looks_like_markdown_ignores_plain_text() -> None:
    assert not looks_like_markdown("你好")
    assert not looks_like_markdown("hello")
    assert not looks_like_markdown("2 * 3 = 6")


def test_send_with_markdown_fallback_uses_markdown_when_detected() -> None:
    async def _run() -> None:
        send_text = RecordingSender(name="text")
        send_markdown = RecordingSender(name="markdown")

        result = await send_with_markdown_fallback(
            content="**hello**",
            msg_id="message-1",
            msg_seq=1,
            send_text=send_text,
            send_markdown=send_markdown,
        )

        assert result == {"id": "markdown"}
        assert send_markdown.calls == [
            {
                "name": "markdown",
                "content": "**hello**",
                "msg_id": "message-1",
                "msg_seq": 1,
            }
        ]
        assert send_text.calls == []

    asyncio.run(_run())


def test_send_with_markdown_fallback_keeps_plain_text_on_text_path() -> None:
    async def _run() -> None:
        send_text = RecordingSender(name="text")
        send_markdown = RecordingSender(name="markdown")

        result = await send_with_markdown_fallback(
            content="hello",
            msg_id="message-1",
            msg_seq=1,
            send_text=send_text,
            send_markdown=send_markdown,
        )

        assert result == {"id": "text"}
        assert send_text.calls[0]["content"] == "hello"
        assert send_markdown.calls == []

    asyncio.run(_run())


def test_send_with_markdown_fallback_forces_markdown_for_keyboard() -> None:
    async def _run() -> None:
        send_text = RecordingSender(name="text")
        send_markdown = RecordingSender(name="markdown")

        result = await send_with_markdown_fallback(
            content="请确认",
            msg_id="message-1",
            msg_seq=1,
            send_text=send_text,
            send_markdown=send_markdown,
            force_markdown=True,
        )

        assert result == {"id": "markdown"}
        assert send_markdown.calls[0]["content"] == "请确认"
        assert send_text.calls == []

    asyncio.run(_run())


def test_send_with_markdown_fallback_retries_text_on_invalid_markdown() -> None:
    async def _run() -> None:
        send_text = RecordingSender(name="text")
        send_markdown = RecordingSender(
            name="markdown",
            error=QQOpenAPIError(
                status_code=400,
                trace_id="trace-1",
                error_code=50055,
                error_message="invalid markdown content",
                known=QQKnownOpenAPIError(
                    50055,
                    "InvalidMarkdownContent",
                    "无效的 markdown content",
                    "request",
                    False,
                ),
            ),
        )

        result = await send_with_markdown_fallback(
            content="**hello**",
            msg_id="message-1",
            msg_seq=3,
            send_text=send_text,
            send_markdown=send_markdown,
        )

        assert result == {"id": "text"}
        assert send_markdown.calls[0]["msg_seq"] == 3
        assert send_text.calls[0]["msg_seq"] == 3

    asyncio.run(_run())


def test_send_with_markdown_fallback_does_not_retry_rate_limit() -> None:
    async def _run() -> None:
        error = QQOpenAPIError(
            status_code=429,
            trace_id="trace-1",
            error_code=22009,
            error_message="msg limit exceed",
            known=QQKnownOpenAPIError(
                22009, "MsgLimitExceed", "消息发送超频", "rate_limit", True
            ),
        )
        send_text = RecordingSender(name="text")
        send_markdown = RecordingSender(name="markdown", error=error)

        try:
            await send_with_markdown_fallback(
                content="**hello**",
                msg_id="message-1",
                msg_seq=1,
                send_text=send_text,
                send_markdown=send_markdown,
            )
        except QQOpenAPIError as exc:
            assert exc.error_code == 22009
        else:
            raise AssertionError("expected rate limit to propagate")

        assert send_text.calls == []

    asyncio.run(_run())


def test_c2c_send_service_sends_markdown_for_formatted_content() -> None:
    async def _run() -> None:
        state = QQSessionState()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        openapi = OpenAPIStub()
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="这是 **重点**",
                channel="qq",
            )
        )

        assert result == {"id": "reply-1"}
        assert openapi.calls == [
            {
                "openid": "user-openid",
                "content": "这是 **重点**",
                "msg_id": "message-1",
                "msg_seq": 1,
                "msg_type": 2,
            }
        ]

    asyncio.run(_run())


def test_c2c_send_service_falls_back_to_text_when_markdown_rejected() -> None:
    async def _run() -> None:
        state = QQSessionState()
        state.latest_message_id_by_session["qq:c2c:user-openid"] = "message-1"
        state.latest_timestamp_by_session["qq:c2c:user-openid"] = (
            "2099-01-01T00:00:00+00:00"
        )
        openapi = MarkdownFallbackOpenAPIStub(
            QQOpenAPIError(
                status_code=400,
                trace_id="trace-1",
                error_code=50056,
                error_message="markdown content forbidden",
                known=QQKnownOpenAPIError(
                    50056,
                    "MarkdownContentForbidden",
                    "不允许发送 markdown content",
                    "permission",
                    False,
                ),
            )
        )
        service = QQC2CSendService(
            channel_name="qq",
            receive_mode="webhook",
            state=state,
            openapi=openapi,
        )

        result = await service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                chat_id="c2c:user-openid",
                content="**hello**",
                channel="qq",
            )
        )

        assert result == {"id": "reply-text"}
        assert openapi.calls == ["markdown", "text"]

    asyncio.run(_run())
