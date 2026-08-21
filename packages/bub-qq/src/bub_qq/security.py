"""Access control, tool policy and rate limiting for the QQ channel.

How the pieces fit together:

- Inbound services consult :class:`QQAccessPolicy` to drop messages from
  senders/groups outside the configured allowlists and to gate comma
  commands (fail-closed: with no configuration, only group owners/admins
  keep command access in groups and nobody keeps it in C2C).
- Inbound adaptation stores QQ metadata on ``ChannelMessage.context``
  under :data:`QQ_CONTEXT_KEY`; the ``load_state`` hook copies it into
  ``TurnState`` under :data:`QQ_STATE_KEY` so the agent-loop interception
  hooks (tool policy, rate limit, audit) can act on it.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from .session import BoundedDict

if TYPE_CHECKING:
    from .config import QQConfig

QQ_CONTEXT_KEY = "_qq"
QQ_STATE_KEY = "qq"

GROUP_PRIVILEGED_ROLES = frozenset({"owner", "admin"})

# The channel reply tool is exempt from every tool policy: sending a reply
# was never gated in direct mode, and denying it under reply_mode="tool"
# would mute the bot entirely.
REPLY_TOOL_NAME = "qq.send"

RESTRICTED_TOOL_PATTERNS: tuple[str, ...] = (
    "bash",
    "bash.*",
    "fs.write",
    "fs.edit",
    "subagent",
)


def _tool_name_forms(tool: str) -> tuple[str, ...]:
    """Both spellings of one tool name.

    Bub exposes registry names to the model with ``.`` replaced by ``_``
    (``fs.write`` -> ``fs_write``) and interception hooks see the
    model-facing form. Matching both keeps dotted config patterns working.
    """

    dotted = tool.replace("_", ".")
    return (tool,) if dotted == tool else (tool, dotted)


def parse_id_list(raw: str) -> frozenset[str]:
    """Parse a comma-separated id list from config into a set."""

    return frozenset(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class QQAccessPolicy:
    """Who may reach the bot and who may run comma commands."""

    admin_users: frozenset[str] = frozenset()
    allow_users: frozenset[str] = frozenset()
    allow_groups: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: QQConfig) -> QQAccessPolicy:
        return cls(
            admin_users=parse_id_list(config.admin_users),
            allow_users=parse_id_list(config.allow_users),
            allow_groups=parse_id_list(config.allow_groups),
        )

    def is_admin_user(self, user_openid: str) -> bool:
        return user_openid in self.admin_users

    def user_allowed(self, user_openid: str) -> bool:
        """C2C sender check. An empty allowlist means no restriction."""

        if not self.allow_users:
            return True
        return user_openid in self.allow_users or user_openid in self.admin_users

    def group_allowed(self, group_openid: str) -> bool:
        """Group check. An empty allowlist means no restriction."""

        if not self.allow_groups:
            return True
        return group_openid in self.allow_groups

    def may_run_command(
        self, *, scope: str, sender_id: str, sender_role: str | None = None
    ) -> bool:
        """Whether this sender may run comma commands (fail-closed)."""

        if self.is_admin_user(sender_id):
            return True
        if scope == "group":
            return (sender_role or "") in GROUP_PRIVILEGED_ROLES
        return False


def denied_tool_reason(
    *,
    tool: str,
    tool_policy: str,
    extra_denied_patterns: frozenset[str] = frozenset(),
) -> str | None:
    """Return a denial message if ``tool`` is blocked under ``tool_policy``."""

    if tool_policy == "open":
        return None
    if tool_policy == "locked":
        return "Tool calls are disabled in this chat."
    patterns = (*RESTRICTED_TOOL_PATTERNS, *extra_denied_patterns)
    forms = _tool_name_forms(tool)
    for pattern in patterns:
        if any(fnmatchcase(form, pattern) for form in forms):
            return f"Tool '{tool}' is not allowed in this chat."
    return None


def evaluate_tool_call(
    config: QQConfig, qq_state: dict[str, Any], tool: str
) -> str | None:
    """Return a denial message for one tool call, or ``None`` to proceed.

    Senders who may run comma commands (configured admins, group
    owners/admins) bypass the tool policy entirely, matching the command
    gate semantics.
    """

    if REPLY_TOOL_NAME in _tool_name_forms(tool):
        return None
    policy = QQAccessPolicy.from_config(config)
    sender_id = str(qq_state.get("sender_id") or "")
    scope = str(qq_state.get("scope") or "")
    sender_role = qq_state.get("sender_role")
    if policy.may_run_command(
        scope=scope,
        sender_id=sender_id,
        sender_role=sender_role if isinstance(sender_role, str) else None,
    ):
        return None
    tool_policy = (
        config.group_tool_policy if scope == "group" else config.c2c_tool_policy
    )
    return denied_tool_reason(
        tool=tool,
        tool_policy=tool_policy,
        extra_denied_patterns=parse_id_list(config.denied_tools),
    )


class SlidingWindowRateLimiter:
    """Per-key sliding window counter with bounded key storage."""

    def __init__(
        self,
        *,
        max_calls: int,
        window_seconds: float,
        max_keys: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._clock = clock
        self._calls: BoundedDict[str, deque[float]] = BoundedDict(max_keys)

    def allow(self, key: str) -> bool:
        """Record one call for ``key``; return False when over the limit."""

        now = self._clock()
        window = self._calls.get(key)
        if window is None:
            window = deque()
        while window and now - window[0] >= self._window_seconds:
            window.popleft()
        allowed = len(window) < self._max_calls
        if allowed:
            window.append(now)
        self._calls[key] = window
        return allowed
