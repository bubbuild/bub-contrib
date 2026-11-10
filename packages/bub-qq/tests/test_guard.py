from __future__ import annotations

from pathlib import Path

import pytest
from bub.hooks.interception import ToolCall

from bub_qq import security
from bub_qq.approval import comma_command_to_call
from bub_qq.config import QQConfig
from bub_qq.guard import Requester
from bub_qq.guard import evaluate
from bub_qq.guard import group_admin_member_ids
from bub_qq.guard import is_admin
from bub_qq.guard import set_registered_admins

ADMIN = Requester(scope="group", sender_id="admin-m", group_openid="g1")
MEMBER = Requester(scope="group", sender_id="member-m", group_openid="g1")


def _config(**overrides: object) -> QQConfig:
    values = {
        "admin_users": "group:g1:admin-m",
        "group_tool_policy": "restricted",
        "c2c_tool_policy": "open",
        "denied_tools": "",
        "group_shell": "approval",
        "c2c_access": "admin_users",
        "state_file": "",
    }
    values.update(overrides)
    return QQConfig.model_construct(**values)


@pytest.fixture(autouse=True)
def _no_registered_admins():
    set_registered_admins(())
    yield
    set_registered_admins(())


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    (tmp_path / "notes.md").write_text("hi", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    return tmp_path


def _decide(tool, args, requester, ws, *, approved=False, **config):
    call = ToolCall(run_id="r", tool=tool, arguments=args)
    return evaluate(
        call, requester, config=_config(**config), workspace=ws, approved=approved
    )


@pytest.mark.parametrize(
    ("tool", "args", "member", "admin"),
    [
        ("bash", {"command": "ls"}, "deny", "approval"),
        ("bash_output", {"shell_id": "1"}, "deny", "allow"),
        ("fs.read", {"path": "notes.md"}, "allow", "allow"),
        ("fs.read", {"path": "/etc/passwd"}, "deny", "deny"),
        ("fs.read", {"path": ".env"}, "deny", "deny"),
        ("fs.write", {"path": "new.md", "content": "x"}, "deny", "allow"),
        ("fs_edit", {"path": "../escape.md"}, "deny", "deny"),
        ("web.fetch", {"url": "https://example.com"}, "allow", "allow"),
        ("subagent", {}, "deny", "deny"),
        ("qq.send", {"content": "hi"}, "allow", "allow"),
    ],
)
def test_group_decision_table(ws, tool, args, member, admin) -> None:
    assert _decide(tool, args, MEMBER, ws).action == member
    assert _decide(tool, args, ADMIN, ws).action == admin


@pytest.mark.parametrize(
    ("tool", "member", "admin"),
    [
        # Code mode runs host Python outside shell approval: never in QQ.
        ("run_code", "deny", "deny"),
        ("code_mode", "deny", "deny"),
        # Known read-only tools stay open to members.
        ("tape.info", "allow", "allow"),
        ("tape_search", "allow", "allow"),
        ("tool.describe", "allow", "allow"),
        ("spill.read", "allow", "allow"),
        ("qq.fetch_attachment", "allow", "allow"),
        # Known session-changing tools are admin-only.
        ("tape.reset", "deny", "allow"),
        ("reasoning_effort", "deny", "allow"),
        # Anything not listed (other plugins, MCP, future Bub tools).
        ("mcp.filesystem.write_file", "deny", "allow"),
        ("web_search.jina_read", "deny", "allow"),
    ],
)
def test_allowlist_in_group(ws, tool, member, admin) -> None:
    assert _decide(tool, {}, MEMBER, ws).action == member
    assert _decide(tool, {}, ADMIN, ws).action == admin


def test_allowed_tools_opens_unknown_tools_to_members(ws) -> None:
    assert _decide("web_search.jina_read", {}, MEMBER, ws).action == "deny"
    assert (
        _decide("web_search.jina_read", {}, MEMBER, ws, allowed_tools="web_search.*").action
        == "allow"
    )
    # allowed_tools cannot unlock shell, code mode or admin-only tools.
    for tool in ("bash", "run_code", "tape.reset"):
        assert _decide(tool, {}, MEMBER, ws, allowed_tools="*").action == "deny"


def test_unknown_tool_still_follows_tool_policy(ws) -> None:
    assert (
        _decide("web_search.jina_read", {}, ADMIN, ws, group_tool_policy="locked").action
        == "deny"
    )
    assert (
        _decide("web_search.jina_read", {}, ADMIN, ws, denied_tools="web_search.*").action
        == "deny"
    )


def test_shell_approval_token_and_deny_setting(ws) -> None:
    assert _decide("bash", {"command": "ls"}, ADMIN, ws, approved=True).action == "allow"
    # A token never lets a non-admin through.
    assert _decide("bash", {"command": "ls"}, MEMBER, ws, approved=True).action == "deny"
    assert (
        _decide("bash", {"command": "ls"}, ADMIN, ws, approved=True, group_shell="deny").action
        == "deny"
    )


def test_locked_policy_blocks_everything_but_replies(ws) -> None:
    for tool, args in (("bash", {"command": "ls"}), ("fs.read", {"path": "notes.md"})):
        assert _decide(tool, args, ADMIN, ws, group_tool_policy="locked").action == "deny"
    assert (
        _decide("qq.send", {"content": "hi"}, MEMBER, ws, group_tool_policy="locked").action
        == "allow"
    )


def test_c2c_access(ws) -> None:
    stranger = Requester(scope="c2c", sender_id="u-2")
    assert _decide("fs.read", {"path": "notes.md"}, stranger, ws).action == "deny"
    assert _decide("qq.send", {"content": "hi"}, stranger, ws).action == "allow"
    assert (
        _decide("fs.read", {"path": "notes.md"}, stranger, ws, c2c_access="allow_users").action
        == "allow"
    )
    c2c_admin = Requester(scope="c2c", sender_id="u-1")
    assert (
        _decide("bash", {"command": "ls"}, c2c_admin, ws, admin_users="c2c:u-1").action
        == "approval"
    )


def test_identity_matching_is_scoped() -> None:
    config = _config(admin_users="group:g1:admin-m,c2c:u-1,legacy-id")
    assert is_admin(config, ADMIN)
    # Same member openid in another group is a different identity.
    assert not is_admin(config, Requester("group", "admin-m", "g2"))
    assert is_admin(config, Requester("c2c", "u-1"))
    assert not is_admin(config, Requester("group", "u-1", "g1"))
    # Bare openids (legacy config) match in any scope.
    assert is_admin(config, Requester("group", "legacy-id", "g9"))
    assert is_admin(config, Requester("c2c", "legacy-id"))
    assert not is_admin(config, Requester("group", "", "g1"))


def test_registered_admins_are_trusted_and_listed() -> None:
    config = _config(admin_users="")
    assert not is_admin(config, MEMBER)
    set_registered_admins({"group:g1:member-m"})
    assert is_admin(config, MEMBER)
    assert set(group_admin_member_ids(_config(admin_users="legacy,c2c:u-1"), "g1")) == {
        "legacy",
        "member-m",
    }


@pytest.mark.parametrize(
    ("line", "tool", "arguments"),
    [
        (",fs.read .env", "fs.read", {"path": ".env"}),
        (",fs.read path=notes.md", "fs.read", {"path": "notes.md"}),
        (",ls -la /", "bash", {"command": "ls -la /"}),
        # Bub looks names up as typed: an alias runs as a shell line.
        (",fs_read notes.md", "bash", {"command": "fs_read notes.md"}),
        (",qq.version", "qq.version", {}),
    ],
)
def test_comma_command_maps_like_bub(line, tool, arguments, agent_tools) -> None:
    call = comma_command_to_call(line, agent_tools)
    assert call.tool == tool
    assert call.arguments == arguments


def test_comma_command_only_knows_the_agents_tools() -> None:
    assert comma_command_to_call(",qq.version", {}).arguments == {"command": "qq.version"}


def test_unparseable_comma_command_is_rejected_like_bub(agent_tools) -> None:
    with pytest.raises(ValueError):
        comma_command_to_call(",echo 'unbalanced", agent_tools)


def test_command_mapping_uses_bub_command_prefix(monkeypatch, agent_tools) -> None:
    monkeypatch.setattr(security, "command_prefix", lambda: "/")
    assert comma_command_to_call("/fs.read notes.md", agent_tools).arguments == {"path": "notes.md"}
    assert comma_command_to_call("/ls", agent_tools).arguments == {"command": "ls"}


def test_comma_fs_read_of_protected_file_is_denied(ws, agent_tools) -> None:
    call = comma_command_to_call(",fs.read .env", agent_tools)
    assert evaluate(call, ADMIN, config=_config(), workspace=ws).action == "deny"
