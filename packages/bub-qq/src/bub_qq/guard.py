"""Guard: the single checkpoint every QQ-initiated action passes through.

Model tool calls and comma commands (both through ``before_tool_call``;
Bub runs a command's tool with the tool hooks) and approved executions all
call :func:`evaluate`. It decides from two things only: who is asking
(:class:`Requester`, trusted when listed as an admin) and which resource
the call touches (:func:`classify`).

Trust never comes from QQ group roles: the deployer is not necessarily the
group owner, so owners and admins of a group are ordinary members unless
the deployer lists them in ``admin_users`` or they register as admins.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from bub.hooks.interception import ToolCall

from .security import REPLY_TOOL_NAME
from .security import denied_tool_reason
from .security import parse_id_list
from .workspace import resolve_in_workspace
from .workspace import tool_protected_reason

if TYPE_CHECKING:
    from .config import QQConfig

type Resource = Literal[
    "reply",
    "shell",
    "shell_session",
    "code",
    "fs_read",
    "fs_write",
    "fetch",
    "safe",
    "admin_only",
    "unknown",
]
type Action = Literal["allow", "deny", "approval"]

_SHELL_TOOLS = frozenset({"bash"})
_SHELL_SESSION_TOOLS = frozenset({"bash.output", "bash.kill"})
_FS_READ_TOOLS = frozenset({"fs.read"})
_FS_WRITE_TOOLS = frozenset({"fs.write", "fs.edit"})
_FETCH_TOOLS = frozenset({"web.fetch"})
# Code mode runs model-written Python as a host process. Approvals execute
# outside the session's code-mode state, so it cannot be approved per call;
# it is refused in QQ sessions.
_CODE_TOOLS = frozenset({"run_code", "code_mode"})
# Known tools that only read session state or this chat: anyone the tool
# policy admits may use them.
_SAFE_TOOLS = frozenset(
    {
        "help",
        "skill",
        "spill.read",
        "tape.anchors",
        "tape.handoff",
        "tape.info",
        "tape.search",
        "tool.describe",
        "qq.fetch_attachment",
        "qq.version",
    }
)
# Known tools that change the session or reach beyond this chat.
_ADMIN_TOOLS = frozenset(
    {"model", "quit", "reasoning_effort", "subagent", "tape.reset"}
)

_CLASSES: tuple[tuple[Resource, frozenset[str]], ...] = (
    ("reply", frozenset({REPLY_TOOL_NAME})),
    ("shell", _SHELL_TOOLS),
    ("shell_session", _SHELL_SESSION_TOOLS),
    ("code", _CODE_TOOLS),
    ("fs_read", _FS_READ_TOOLS),
    ("fs_write", _FS_WRITE_TOOLS),
    ("fetch", _FETCH_TOOLS),
    ("safe", _SAFE_TOOLS),
    ("admin_only", _ADMIN_TOOLS),
)

_registered_admins: frozenset[str] = frozenset()


def set_registered_admins(identities: Iterable[str]) -> None:
    """Admins registered at runtime (pairing), merged with ``admin_users``."""

    global _registered_admins
    _registered_admins = frozenset(identities)


def registered_admins() -> frozenset[str]:
    return _registered_admins


@dataclass(frozen=True)
class Requester:
    """Who is asking, in the scope they asked from.

    The same person has a different openid in C2C (``user_openid``) and in
    each group (``member_openid``), so identities are scoped.
    """

    scope: Literal["group", "c2c"]
    sender_id: str
    group_openid: str = ""
    # Other senders whose messages Bub merged into the same turn. Bub keeps
    # only the last message's metadata, so without these a member's request
    # could run with the rights of an admin who spoke right after it.
    co_senders: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        if self.scope == "group":
            return f"group:{self.group_openid}:{self.sender_id}"
        return f"c2c:{self.sender_id}"

    @classmethod
    def from_state(cls, qq_state: dict[str, Any]) -> Requester:
        scope = "group" if str(qq_state.get("scope") or "") == "group" else "c2c"
        sender_id = str(qq_state.get("sender_id") or "")
        batch = qq_state.get("batch_senders") or ()
        return cls(
            scope=scope,
            sender_id=sender_id,
            group_openid=str(qq_state.get("group_openid") or ""),
            co_senders=tuple(str(item) for item in batch if str(item) != sender_id),
        )


def admin_entries(config: QQConfig) -> frozenset[str]:
    return parse_id_list(config.admin_users) | _registered_admins


def is_admin(config: QQConfig, requester: Requester) -> bool:
    """Scoped identities match exactly; bare openids (legacy) match any scope.

    A turn carrying several senders' messages is an admin turn only when
    every one of them is an admin: it acts with its least-trusted sender.
    """

    entries = admin_entries(config)
    for sender_id in (requester.sender_id, *requester.co_senders):
        if not sender_id:
            return False
        one = Requester(requester.scope, sender_id, requester.group_openid)
        if one.identity not in entries and sender_id not in entries:
            return False
    return True


def group_admin_member_ids(config: QQConfig, group_openid: str) -> list[str]:
    """``member_openid`` values of admins known for one group."""

    prefix = f"group:{group_openid}:"
    ids: list[str] = []
    for entry in sorted(admin_entries(config)):
        if entry.startswith(prefix):
            ids.append(entry.removeprefix(prefix))
        elif ":" not in entry:
            ids.append(entry)
    return ids


@dataclass(frozen=True)
class Decision:
    action: Action
    resource: Resource
    reason: str = ""
    # A denial the caller can fix by changing the call (a path outside the
    # workspace); every other denial is a permission rule that holds.
    fixable: bool = False

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


def canonical_tool(tool: str) -> str:
    """Registry name for a tool (``fs_read`` → ``fs.read``)."""

    return tool.replace("_", ".", 1) if "." not in tool and "_" in tool else tool


def _name_forms(tool: str) -> frozenset[str]:
    """Registry and model-facing spellings (``fs_read`` → ``fs.read``).

    Some registry names contain ``_`` themselves (``run_code``), so the raw
    name is always kept alongside the dotted forms.
    """

    return frozenset({tool, tool.replace("_", ".", 1), tool.replace("_", ".")})


def classify(call: ToolCall, *, extra_safe_patterns: Iterable[str] = ()) -> Resource:
    """Resource class of one call. Tools nobody listed are ``unknown``."""

    forms = _name_forms(call.tool)
    for resource, names in _CLASSES:
        if forms & names:
            return resource
    if any(fnmatchcase(form, pattern) for form in forms for pattern in extra_safe_patterns):
        return "safe"
    return "unknown"


def evaluate(
    call: ToolCall,
    requester: Requester,
    *,
    config: QQConfig,
    workspace: Path,
    protected: Iterable[Path] = (),
    approved: bool = False,
) -> Decision:
    """Decide one call. ``approved`` means a valid approval token was used."""

    resource = classify(call, extra_safe_patterns=parse_id_list(config.allowed_tools))
    protected_reason = tool_protected_reason(call, workspace, extra=tuple(protected))
    if protected_reason is not None:
        return Decision("deny", resource, protected_reason)
    if resource == "reply":
        return Decision("allow", resource)

    admin = is_admin(config, requester)
    if requester.scope == "c2c" and not admin and config.c2c_access == "admin_users":
        return Decision(
            "deny",
            resource,
            "Tools are only available to admins in private chat.",
        )

    tool_policy = (
        config.group_tool_policy if requester.scope == "group" else config.c2c_tool_policy
    )
    if tool_policy == "locked":
        return Decision("deny", resource, "Tool calls are disabled in this chat.")

    if resource == "code":
        return Decision(
            "deny",
            resource,
            "Code mode (run_code) is not available in QQ chats; use bash, which needs admin approval.",
        )

    if resource == "shell":
        if not admin:
            return Decision("deny", resource, "Shell commands are admin-only.")
        if config.group_shell == "deny":
            return Decision("deny", resource, "Shell commands are disabled (group_shell=deny).")
        if approved:
            return Decision("allow", resource)
        return Decision("approval", resource, "Shell commands need admin approval.")

    if resource == "shell_session":
        # Reading or stopping a shell that an approved command started.
        if not admin:
            return Decision("deny", resource, "Shell commands are admin-only.")
        return Decision("allow", resource)

    if resource in {"fs_read", "fs_write"}:
        if resource == "fs_write" and not admin:
            return Decision("deny", resource, "Writing files is admin-only.")
        raw = (call.arguments or {}).get("path") if isinstance(call.arguments, dict) else None
        if raw is not None and str(raw).strip():
            if resolve_in_workspace(str(raw), workspace) is None:
                return Decision(
                    "deny",
                    resource,
                    f"path '{raw}' is outside the workspace ({workspace}).",
                    fixable=True,
                )
        return Decision("allow", resource)

    if resource in {"admin_only", "unknown"} and not admin:
        if resource == "unknown":
            reason = (
                f"Tool '{call.tool}' is admin-only here: it is not on the QQ allowlist"
                " (an admin can add it to allowed_tools)."
            )
        else:
            reason = f"Tool '{call.tool}' is admin-only."
        return Decision("deny", resource, reason)

    reason = denied_tool_reason(
        tool=call.tool,
        tool_policy=tool_policy,
        extra_denied_patterns=parse_id_list(config.denied_tools),
    )
    if reason is not None:
        return Decision("deny", resource, reason)
    return Decision("allow", resource)
