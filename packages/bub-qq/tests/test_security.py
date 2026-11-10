from __future__ import annotations

from bub.channels.message import ChannelMessage

from bub_qq import plugin
from bub_qq.config import QQConfig
from bub_qq.security import QQ_CONTEXT_KEY
from bub_qq.security import QQAccessPolicy
from bub_qq.security import SlidingWindowRateLimiter
from bub_qq.security import denied_tool_reason
from bub_qq.security import parse_id_list


def _config(**overrides: object) -> QQConfig:
    return QQConfig.model_construct(**overrides)


def test_parse_id_list_strips_and_drops_empty_items() -> None:
    assert parse_id_list(" a, b ,,c ") == {"a", "b", "c"}
    assert parse_id_list("") == frozenset()


def test_access_policy_allowlists_are_fail_closed_when_configured() -> None:
    policy = QQAccessPolicy(
        allow_users=frozenset({"user-1"}),
        allow_groups=frozenset({"group-1"}),
    )

    assert policy.user_allowed("user-1") is True
    assert policy.user_allowed("stranger") is False
    assert policy.group_allowed("group-1") is True
    assert policy.group_allowed("other-group") is False

    open_policy = QQAccessPolicy()
    assert open_policy.user_allowed("anyone") is True
    assert open_policy.group_allowed("any-group") is True


def test_denied_tool_reason_by_policy_tier() -> None:
    assert denied_tool_reason(tool="bash", tool_policy="open") is None
    assert denied_tool_reason(tool="bash", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="bash.kill", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="fs.write", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="fs.edit", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="subagent", tool_policy="restricted") is not None
    assert denied_tool_reason(tool="fs.read", tool_policy="restricted") is None
    assert denied_tool_reason(tool="web.fetch", tool_policy="restricted") is None
    assert denied_tool_reason(tool="fs.read", tool_policy="locked") is not None


def test_denied_tool_reason_extra_patterns() -> None:
    extra = parse_id_list("web.fetch,tape.*")

    assert (
        denied_tool_reason(
            tool="web.fetch", tool_policy="restricted", extra_denied_patterns=extra
        )
        is not None
    )
    assert (
        denied_tool_reason(
            tool="tape.reset", tool_policy="restricted", extra_denied_patterns=extra
        )
        is not None
    )
    assert (
        denied_tool_reason(
            tool="fs.read", tool_policy="restricted", extra_denied_patterns=extra
        )
        is None
    )


def test_rate_limiter_sliding_window() -> None:
    now = 0.0
    limiter = SlidingWindowRateLimiter(
        max_calls=2, window_seconds=60.0, clock=lambda: now
    )

    assert limiter.allow("key") is True
    assert limiter.allow("key") is True
    assert limiter.allow("key") is False
    assert limiter.allow("other-key") is True

    now = 61.0
    assert limiter.allow("key") is True


def test_load_state_hook_injects_qq_context() -> None:
    message = ChannelMessage(
        session_id="qq:group:group-1",
        channel="qq",
        chat_id="group:group-1",
        content='{"message": "hi", "sender_id": "member-1"}',
        context={
            QQ_CONTEXT_KEY: {
                "scope": "group",
                "sender_id": "member-1",
                "sender_role": "admin",
            }
        },
    )

    state = plugin.load_state(message, "qq:group:group-1")

    assert state == {
        "qq": {
            "scope": "group",
            "sender_id": "member-1",
            "sender_role": "admin",
            "session_id": "qq:group:group-1",
            "batch_senders": ["member-1"],
        }
    }


def test_load_state_hook_ignores_non_qq_messages() -> None:
    message = ChannelMessage(
        session_id="other:session",
        channel="other",
        chat_id="default",
        content="hi",
    )

    assert plugin.load_state(message, "other:session") is None
