from __future__ import annotations

import asyncio

import bub
from bub.channels.message import ChannelMessage
from bub.hooks.interception import LLMCallResult
from bub.hooks.interception import ToolCall
from bub.hooks.interception import ToolCallResult
from bub.core.tools import ToolContext

from bub_qq import plugin
from bub_qq import runtime
from bub_qq import tools
from bub_qq.config import QQConfig
from bub_qq.inbound.c2c import build_c2c_channel_message
from bub_qq.inbound.group import build_group_channel_message
from bub_qq.outbound.c2c import QQC2CSendService
from bub_qq.outbound.group import QQGroupSendService
from bub_qq.outbound.send_flow import is_no_reply
from bub_qq.outbound.send_flow import normalize_outbound_content
from bub_qq.protocol.models import QQC2CMessage
from bub_qq.protocol.models import QQGroupMessage
from bub_qq.security import denied_tool_reason
from bub_qq.security import parse_id_list
from bub_qq.session import QQSessionState


def _config(**overrides: object) -> QQConfig:
    return QQConfig.model_construct(**overrides)


def _group_message(content: str = "hello") -> QQGroupMessage:
    return QQGroupMessage(
        message_id="group-message-1",
        group_openid="group-openid",
        member_openid="member-openid",
        sender_name="Alice",
        content=content,
        timestamp="2099-01-01T00:00:00+00:00",
        attachments=(),
        mentions=(),
        event_id="event-1",
        sequence=1,
        event_type="GROUP_MESSAGE_CREATE",
        member_role="member",
    )


def _c2c_message(content: str = "hello") -> QQC2CMessage:
    return QQC2CMessage(
        message_id="c2c-message-1",
        user_openid="user-openid",
        content=content,
        timestamp="2099-01-01T00:00:00+00:00",
        attachments=(),
        event_id="event-1",
        sequence=1,
    )


class RecordingOpenAPI:
    """Fails the test if any send endpoint is reached."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def post_group_text_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("group_text")
        return {"id": "x"}

    async def post_group_markdown_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("group_markdown")
        return {"id": "x"}

    async def post_group_active_text_message(
        self, **kwargs: object
    ) -> dict[str, object]:
        self.calls.append("group_active")
        return {"id": "x"}

    async def post_c2c_text_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("c2c_text")
        return {"id": "x"}

    async def post_c2c_markdown_message(self, **kwargs: object) -> dict[str, object]:
        self.calls.append("c2c_markdown")
        return {"id": "x"}


def test_normalize_strips_edge_special_tokens() -> None:
    assert normalize_outbound_content("<|eos|>") == ""
    assert normalize_outbound_content("<|im_end|>") == ""
    assert normalize_outbound_content("<|eos|><|endoftext|>") == ""
    assert normalize_outbound_content("你好<|eos|>") == "你好"
    assert normalize_outbound_content("<|im_start|>你好") == "你好"
    assert normalize_outbound_content("<no_reply/><|eos|>") == "<no_reply/>"
    # Tokens in the middle of real content are left alone.
    assert normalize_outbound_content("a <|eos|> b") == "a <|eos|> b"


def test_group_send_skips_pure_special_token_output() -> None:
    openapi = RecordingOpenAPI()
    service = QQGroupSendService(
        channel_name="qq",
        receive_mode="websocket",
        state=QQSessionState(),
        openapi=openapi,
    )

    result = asyncio.run(
        service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                channel="qq",
                chat_id="group:group-openid",
                content="<|eos|>",
            )
        )
    )

    assert result is None
    assert openapi.calls == []


def test_is_no_reply_variants() -> None:
    assert is_no_reply("<no_reply/>") is True
    assert is_no_reply("  <no_reply/>  ") is True
    assert is_no_reply("<no_reply />") is True
    assert is_no_reply("<no_reply>") is True
    assert is_no_reply("<NO_REPLY/>") is True
    assert is_no_reply("<no_reply/> (nothing to add)") is True
    assert is_no_reply("no_reply") is False
    assert is_no_reply("I will reply: <no_reply/> is the sentinel") is False
    assert is_no_reply("") is False


def test_group_send_skips_no_reply_sentinel() -> None:
    openapi = RecordingOpenAPI()
    service = QQGroupSendService(
        channel_name="qq",
        receive_mode="websocket",
        state=QQSessionState(),
        openapi=openapi,
    )

    result = asyncio.run(
        service.send(
            ChannelMessage(
                session_id="qq:group:group-openid",
                channel="qq",
                chat_id="group:group-openid",
                content="<no_reply/>",
            )
        )
    )

    assert result is None
    assert openapi.calls == []


def test_c2c_send_skips_no_reply_sentinel() -> None:
    openapi = RecordingOpenAPI()
    service = QQC2CSendService(
        channel_name="qq",
        receive_mode="websocket",
        state=QQSessionState(),
        openapi=openapi,
    )

    result = asyncio.run(
        service.send(
            ChannelMessage(
                session_id="qq:c2c:user-openid",
                channel="qq",
                chat_id="c2c:user-openid",
                content="  <no_reply/>",
            )
        )
    )

    assert result is None
    assert openapi.calls == []


def test_group_inbound_direct_mode_keeps_direct_output() -> None:
    message = build_group_channel_message("qq", _group_message())

    assert message.output_channel == "qq"


def test_group_inbound_tool_mode_suppresses_direct_output() -> None:
    message = build_group_channel_message(
        "qq", _group_message(), suppress_direct_output=True
    )

    assert message.output_channel == "null"


def test_group_inbound_tool_mode_keeps_command_output_direct() -> None:
    message = build_group_channel_message(
        "qq",
        _group_message(content=",help"),
        allow_command=True,
        suppress_direct_output=True,
    )

    assert message.kind == "command"
    assert message.output_channel == "qq"


def test_c2c_inbound_tool_mode_suppresses_direct_output() -> None:
    default_message = build_c2c_channel_message("qq", _c2c_message())
    suppressed = build_c2c_channel_message(
        "qq", _c2c_message(), suppress_direct_output=True
    )
    command = build_c2c_channel_message(
        "qq",
        _c2c_message(content=",help"),
        allow_command=True,
        suppress_direct_output=True,
    )

    assert default_message.output_channel == "qq"
    assert suppressed.output_channel == "null"
    assert command.output_channel == "qq"


def test_system_prompt_hook_ignores_non_qq_sessions() -> None:
    assert plugin.system_prompt("hi", {}) is None


def test_system_prompt_hook_direct_mode(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="direct"))

    result = plugin.system_prompt("hi", {"qq": {"scope": "group"}})

    assert result is not None
    assert plugin.DIRECT_REPLY_PROMPT in result
    assert "<no_reply/>" in result
    assert "<qq_workspace>" in result


def test_system_prompt_hook_tool_mode(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))

    result = plugin.system_prompt("hi", {"qq": {"scope": "group"}})

    assert result is not None
    assert plugin.TOOL_REPLY_PROMPT in result
    assert "qq.send" in result
    assert "<qq_workspace>" in result


def test_reply_tool_exempt_from_tool_policy(tmp_path) -> None:
    from bub.hooks.interception import ToolCall

    from bub_qq.guard import Requester
    from bub_qq.guard import evaluate

    config = _config(
        admin_users="",
        denied_tools="",
        group_tool_policy="locked",
        c2c_access="admin_users",
        group_shell="approval",
    )
    member = Requester(scope="group", sender_id="member-1", group_openid="g")

    def decide(tool: str) -> str:
        call = ToolCall(run_id="r", tool=tool, arguments={"content": "hi"})
        return evaluate(call, member, config=config, workspace=tmp_path).action

    assert decide("qq.send") == "allow"
    assert decide("qq_send") == "allow"
    assert decide("fs.read") == "deny"


def test_denied_tool_reason_matches_model_facing_aliases() -> None:
    assert denied_tool_reason(tool="fs_write", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="fs_edit", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="bash_output", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="bash_kill", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="fs_read", tool_policy="restricted") is None
    assert (
        denied_tool_reason(
            tool="tape_reset",
            tool_policy="restricted",
            extra_denied_patterns=parse_id_list("tape.*"),
        )
        is not None
    )


def test_turn_declined_reply_detection() -> None:
    final_without_tools = LLMCallResult(run_id="r1")
    step_with_tools = LLMCallResult(
        run_id="r1",
        tool_calls=[{"function": {"name": "qq_send", "arguments": "{}"}}],
    )
    failed = LLMCallResult(run_id="r1", error=RuntimeError("boom"))

    assert plugin._turn_declined_reply({}, final_without_tools) is True
    assert plugin._turn_declined_reply({}, step_with_tools) is False
    assert plugin._turn_declined_reply({}, failed) is False
    assert (
        plugin._turn_declined_reply({"replied_via_tool": True}, final_without_tools)
        is False
    )


def test_after_tool_call_marks_reply_tool_usage() -> None:
    state = {"qq": {"scope": "group", "session_id": "qq:group:g", "sender_id": "u"}}
    call = ToolCall(run_id="r1", tool="qq_send", arguments={"content": "hi"})
    result = ToolCallResult(
        run_id="r1", tool="qq_send", arguments={"content": "hi"}, result="Sent."
    )

    plugin.after_tool_call(call, result, state)

    assert state["qq"]["replied_via_tool"] is True


def test_after_tool_call_ignores_other_tools_for_reply_flag() -> None:
    state = {"qq": {"scope": "group", "session_id": "qq:group:g", "sender_id": "u"}}
    call = ToolCall(run_id="r1", tool="tape.info", arguments={})
    result = ToolCallResult(run_id="r1", tool="tape.info", arguments={}, result="ok")

    plugin.after_tool_call(call, result, state)

    assert "replied_via_tool" not in state["qq"]


class FakeChannel:
    name = "qq"

    def __init__(self, result: dict[str, object] | None) -> None:
        self.result = result
        self.messages: list[ChannelMessage] = []

    async def send_for_result(
        self, message: ChannelMessage
    ) -> dict[str, object] | None:
        self.messages.append(message)
        return self.result


def _tool_context(qq_state: dict[str, object] | None) -> ToolContext:
    state: dict[str, object] = {}
    if qq_state is not None:
        state["qq"] = qq_state
    return ToolContext(tape=None, state=state)


def _run_qq_send(content: str, context: ToolContext) -> tools.QQSendResult:
    return asyncio.run(tools.qq_send.run(content=content, context=context))


def test_qq_send_tool_declares_output_schema() -> None:
    schema = tools.qq_send.output_schema

    assert schema is not None
    assert schema["properties"]["status"]["enum"] == [
        "sent",
        "pending_audit",
        "already_sent",
        "not_sent",
    ]


def test_qq_send_tool_disabled_in_direct_mode(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="direct"))

    result = _run_qq_send("hi", _tool_context({"scope": "c2c"}))

    assert result["status"] == "not_sent"
    assert tools.qq_send.render(result) == (
        "Not sent: qq.send is disabled (qq.reply_mode is 'direct')."
        " Write your reply as plain text instead."
    )


def test_qq_send_tool_requires_qq_session(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))

    result = _run_qq_send("hi", _tool_context(None))

    assert result == {
        "status": "not_sent",
        "reason": "qq.send is only available inside QQ channel sessions.",
    }


def test_qq_send_tool_sends_via_channel(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    channel = FakeChannel({"id": "reply-1"})
    runtime.set_active_channel(channel)
    try:
        result = _run_qq_send(
            "hello group",
            _tool_context(
                {
                    "scope": "group",
                    "group_openid": "group-openid",
                    "sender_id": "member-openid",
                    "session_id": "qq:group:group-openid",
                }
            ),
        )
    finally:
        runtime.set_active_channel(None)

    assert result == {"status": "sent"}
    assert tools.qq_send.render(result) == "Sent."
    assert len(channel.messages) == 1
    sent = channel.messages[0]
    assert sent.session_id == "qq:group:group-openid"
    assert sent.chat_id == "group:group-openid"
    assert sent.content == "hello group"


def test_qq_send_tool_attaches_at_user_ids(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    channel = FakeChannel({"id": "at-1"})
    runtime.set_active_channel(channel)
    try:
        result = asyncio.run(
            tools.qq_send.run(
                content="请看下",
                at_user_ids=["member-openid"],
                context=_tool_context(
                    {
                        "scope": "group",
                        "group_openid": "group-openid",
                        "sender_id": "member-openid",
                        "session_id": "qq:group:group-openid",
                    }
                ),
            )
        )
    finally:
        runtime.set_active_channel(None)

    text = tools.qq_send.render(result)
    assert text == "Sent."
    outbound = channel.messages[0].context["_qq_outbound"]
    assert outbound["at_user_ids"] == ["member-openid"]


def test_qq_send_tool_attaches_media_url(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    channel = FakeChannel({"id": "media-1"})
    runtime.set_active_channel(channel)
    try:
        result = asyncio.run(
            tools.qq_send.run(
                content="",
                media_url="https://example.com/pic.png",
                context=_tool_context(
                    {
                        "scope": "group",
                        "group_openid": "group-openid",
                        "sender_id": "member-openid",
                        "session_id": "qq:group:group-openid",
                    }
                ),
            )
        )
    finally:
        runtime.set_active_channel(None)

    text = tools.qq_send.render(result)
    assert text == "Sent."
    sent = channel.messages[0]
    outbound = sent.context.get("_qq_outbound")
    assert isinstance(outbound, dict)
    assert outbound["media_url"] == "https://example.com/pic.png"
    assert outbound["file_type"] == 1


def test_qq_send_tool_rejects_media_path_outside_workspace(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    channel = FakeChannel({"id": "x"})
    runtime.set_active_channel(channel)
    context = _tool_context(
        {
            "scope": "group",
            "group_openid": "group-openid",
            "sender_id": "member-openid",
            "session_id": "qq:group:group-openid",
        }
    )
    context.state["_runtime_workspace"] = str(tmp_path)
    try:
        result = asyncio.run(
            tools.qq_send.run(
                content="",
                media_path="/etc/hosts",
                context=context,
            )
        )
    finally:
        runtime.set_active_channel(None)

    text = tools.qq_send.render(result)
    assert "outbox" in text
    assert channel.messages == []


def test_qq_send_tool_rejects_bad_media_url(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    channel = FakeChannel({"id": "x"})
    runtime.set_active_channel(channel)
    try:
        result = asyncio.run(
            tools.qq_send.run(
                content="",
                media_url="ftp://example.com/pic.png",
                context=_tool_context(
                    {
                        "scope": "c2c",
                        "sender_id": "user-openid",
                        "session_id": "qq:c2c:user-openid",
                    }
                ),
            )
        )
    finally:
        runtime.set_active_channel(None)

    text = tools.qq_send.render(result)
    assert text.startswith("Not sent")
    assert channel.messages == []


def test_qq_send_tool_reports_statuses(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    qq_state = {
        "scope": "c2c",
        "sender_id": "user-openid",
        "session_id": "qq:c2c:user-openid",
    }

    for channel_result, expected_status, expected_text in [
        (
            None,
            "not_sent",
            (
                "Not sent: the QQ channel skipped this send"
                " (empty content, closed reply window, or no remaining passive replies;"
                " see gateway logs). Do not retry with identical content."
            ),
        ),
        (
            {"status": "failed", "error_code": 22009, "error": "msg limit exceed"},
            "not_sent",
            (
                "Not sent: QQ OpenAPI error code=22009 msg limit exceed."
                " Do not retry with identical content."
            ),
        ),
        (
            {"status": "pending_audit"},
            "pending_audit",
            "Accepted: QQ queued the message for manual review before delivery.",
        ),
        (
            {"status": "already_sent"},
            "already_sent",
            "Skipped: identical content was already sent for this reply window.",
        ),
    ]:
        channel = FakeChannel(channel_result)
        runtime.set_active_channel(channel)
        try:
            result = _run_qq_send("hi", _tool_context(qq_state))
        finally:
            runtime.set_active_channel(None)
        assert result["status"] == expected_status
        assert tools.qq_send.render(result) == expected_text
        assert channel.messages[0].chat_id == "c2c:user-openid"


def test_qq_send_tool_explains_media_upload_timeout(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    channel = FakeChannel(
        {
            "status": "failed",
            "error_code": 850027,
            "error": "富媒体文件上传超时",
        }
    )
    runtime.set_active_channel(channel)
    try:
        result = asyncio.run(
            tools.qq_send.run(
                content="",
                media_url="https://i.imgur.com/pic.png",
                context=_tool_context(
                    {
                        "scope": "group",
                        "group_openid": "group-openid",
                        "sender_id": "member-openid",
                        "session_id": "qq:group:group-openid",
                    }
                ),
            )
        )
    finally:
        runtime.set_active_channel(None)

    text = tools.qq_send.render(result)
    assert "850027" in text
    assert "media_path" in text
    assert "Do not retry the same failed payload" in text


def test_qq_send_tool_explains_media_download_failure(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    channel = FakeChannel(
        {
            "status": "failed",
            "error": "failed to download media_url (https://i.imgur.com/pic.png): 404",
        }
    )
    runtime.set_active_channel(channel)
    try:
        result = asyncio.run(
            tools.qq_send.run(
                content="",
                media_url="https://i.imgur.com/pic.png",
                context=_tool_context(
                    {
                        "scope": "group",
                        "group_openid": "group-openid",
                        "sender_id": "member-openid",
                        "session_id": "qq:group:group-openid",
                    }
                ),
            )
        )
    finally:
        runtime.set_active_channel(None)

    text = tools.qq_send.render(result)
    assert "failed to download media_url" in text
    assert "Do not retry the same media_url" in text


def test_qq_send_tool_without_running_channel(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    runtime.set_active_channel(None)

    result = _run_qq_send("hi", _tool_context({"scope": "c2c"}))

    assert tools.qq_send.render(result) == (
        "Not sent: the QQ channel is not running in this process."
    )
