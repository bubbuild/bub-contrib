from __future__ import annotations

import asyncio

import bub
from bub.channels.message import ChannelMessage
from bub.hooks.interception import LlmCallResult
from bub.hooks.interception import ToolCall
from bub.hooks.interception import ToolCallResult
from bub.tools import ToolContext

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
from bub_qq.security import evaluate_tool_call
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

    assert result == plugin.DIRECT_REPLY_PROMPT
    assert "<no_reply/>" in result


def test_system_prompt_hook_tool_mode(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))

    result = plugin.system_prompt("hi", {"qq": {"scope": "group"}})

    assert result == plugin.TOOL_REPLY_PROMPT
    assert "qq.send" in result


def test_reply_tool_exempt_from_tool_policy() -> None:
    config = _config(admin_users="", denied_tools="", group_tool_policy="locked")
    qq_state = {"scope": "group", "sender_id": "member-1", "sender_role": "member"}

    assert evaluate_tool_call(config, qq_state, "qq.send") is None
    assert evaluate_tool_call(config, qq_state, "qq_send") is None
    assert evaluate_tool_call(config, qq_state, "fs.read") is not None


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
    final_without_tools = LlmCallResult(run_id="r1")
    step_with_tools = LlmCallResult(
        run_id="r1",
        tool_calls=[{"function": {"name": "qq_send", "arguments": "{}"}}],
    )
    failed = LlmCallResult(run_id="r1", error=RuntimeError("boom"))

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


def _run_qq_send(content: str, context: ToolContext) -> str:
    return asyncio.run(tools.qq_send.run(content=content, context=context))


def test_qq_send_tool_disabled_in_direct_mode(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="direct"))

    result = _run_qq_send("hi", _tool_context({"scope": "c2c"}))

    assert result.startswith("Not sent")
    assert "direct" in result


def test_qq_send_tool_requires_qq_session(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))

    result = _run_qq_send("hi", _tool_context(None))

    assert result.startswith("Not sent")


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

    assert result == "Sent."
    assert len(channel.messages) == 1
    sent = channel.messages[0]
    assert sent.session_id == "qq:group:group-openid"
    assert sent.chat_id == "group:group-openid"
    assert sent.content == "hello group"


def test_qq_send_tool_reports_statuses(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    qq_state = {
        "scope": "c2c",
        "sender_id": "user-openid",
        "session_id": "qq:c2c:user-openid",
    }

    for channel_result, expected_prefix in [
        (None, "Not sent"),
        ({"status": "pending_audit"}, "Accepted"),
        ({"status": "already_sent"}, "Skipped"),
    ]:
        channel = FakeChannel(channel_result)
        runtime.set_active_channel(channel)
        try:
            result = _run_qq_send("hi", _tool_context(qq_state))
        finally:
            runtime.set_active_channel(None)
        assert result.startswith(expected_prefix)
        assert channel.messages[0].chat_id == "c2c:user-openid"


def test_qq_send_tool_without_running_channel(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(reply_mode="tool"))
    runtime.set_active_channel(None)

    result = _run_qq_send("hi", _tool_context({"scope": "c2c"}))

    assert result.startswith("Not sent")
    assert "not running" in result
