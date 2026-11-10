from __future__ import annotations

import asyncio
from pathlib import Path

import bub
import pytest
from bub.channels.message import ChannelMessage
from bub.hooks.interception import ToolCall

from bub_qq import plugin
from bub_qq.approval import reset_approval_state
from bub_qq.config import QQConfig
from bub_qq.runtime import set_active_channel
from bub_qq.security import QQ_STATE_KEY
from bub_qq.workspace import sensitive_path_reason


class FakeChannel:
    name = "qq"

    def __init__(self) -> None:
        self.messages: list[ChannelMessage] = []

    async def send_for_result(self, message: ChannelMessage) -> dict[str, object]:
        self.messages.append(message)
        return {"id": "sent"}


def _config(**overrides: object) -> QQConfig:
    values = {
        "admin_users": "admin-1",
        "group_tool_policy": "open",
        "c2c_tool_policy": "open",
        "group_shell": "approval",
        "c2c_access": "admin_users",
        "denied_tools": "",
        "state_file": "",
    }
    values.update(overrides)
    return QQConfig.model_construct(**values)


def _state(workspace: Path, *, sender: str = "member-1", scope: str = "group") -> dict:
    return {
        QQ_STATE_KEY: {
            "scope": scope,
            "sender_id": sender,
            "sender_role": "owner",
            "group_openid": "group-1",
            "session_id": "qq:group:group-1",
        },
        "session_id": "qq:group:group-1",
        "_runtime_workspace": str(workspace),
    }


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / ".env").write_text("BUB_QQ_SECRET=s3cret", encoding="utf-8")
    (tmp_path / ".env.local").write_text("KEY=1", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]", encoding="utf-8")
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    (tmp_path / "harmless.txt").symlink_to(tmp_path / ".env")
    return tmp_path


@pytest.fixture(autouse=True)
def _reset():
    reset_approval_state()
    set_active_channel(None)
    yield
    reset_approval_state()
    set_active_channel(None)


def _decide(call: ToolCall, state: dict):
    return asyncio.run(plugin.before_tool_call(call, state))


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("fs.read", {"path": ".env"}),
        ("fs_read", {"path": ".env.local"}),
        ("fs.write", {"path": ".git/config", "content": "x"}),
        ("fs_edit", {"path": "harmless.txt", "old": "a", "new": "b"}),
        ("qq_send", {"content": "", "media_path": ".env"}),
    ],
)
def test_protected_files_are_denied_even_for_admins(
    monkeypatch, workspace: Path, tool: str, arguments: dict
) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config())
    channel = FakeChannel()
    set_active_channel(channel)
    # A configured admin: trust never unlocks protected files.
    state = _state(workspace, sender="admin-1")

    decision = _decide(ToolCall(run_id="r", tool=tool, arguments=arguments), state)

    assert decision is not None
    assert decision.action == "deny"
    assert "protected" in (decision.message or "")
    # Refused outright: no approval keyboard was posted to the group.
    assert channel.messages == []


def test_state_file_is_protected(monkeypatch, workspace: Path) -> None:
    state_file = workspace / "qq-state.json"
    state_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        bub, "ensure_config", lambda cls: _config(state_file=str(state_file))
    )
    decision = _decide(
        ToolCall(run_id="r", tool="fs.read", arguments={"path": "qq-state.json"}),
        _state(workspace),
    )
    assert decision is not None and decision.action == "deny"


def test_qq_send_media_outside_outbox_is_denied(monkeypatch, workspace: Path) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config())
    decision = _decide(
        ToolCall(
            run_id="r", tool="qq.send", arguments={"content": "", "media_path": "notes.md"}
        ),
        _state(workspace),
    )
    assert decision is not None and decision.action == "deny"
    assert "outbox" in (decision.message or "")


def test_ordinary_files_still_pass(monkeypatch, workspace: Path) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config())
    (workspace / "outbox").mkdir()
    (workspace / "outbox" / "chart.png").write_bytes(b"png")
    assert (
        _decide(
            ToolCall(run_id="r", tool="fs.read", arguments={"path": "notes.md"}),
            _state(workspace),
        )
        is None
    )
    assert (
        _decide(
            ToolCall(
                run_id="r",
                tool="qq.send",
                arguments={"content": "", "media_path": "outbox/chart.png"},
            ),
            _state(workspace),
        )
        is None
    )


def test_sensitive_path_reason_matches_names_not_substrings(tmp_path: Path) -> None:
    assert sensitive_path_reason(tmp_path / ".env") is not None
    assert sensitive_path_reason(tmp_path / ".env.production") is not None
    assert sensitive_path_reason(tmp_path / "a" / ".git" / "HEAD") is not None
    assert sensitive_path_reason(tmp_path / "env.md") is None
    assert sensitive_path_reason(tmp_path / ".envrc") is None
    assert sensitive_path_reason(tmp_path / ".github" / "ci.yml") is None
