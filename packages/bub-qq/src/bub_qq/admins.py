"""Admin registration by one-time pairing code (``,qq.claim``).

Trust only comes from the deployer, and the deployer is whoever can read
the server log. So when no admin exists, the plugin logs a pairing code;
sending ``,qq.claim <code>`` in C2C registers that sender as an admin.
An admin then registers their identity in a group with ``,qq.claim
group`` (C2C) followed by ``,qq.claim <group code>`` in that group,
because QQ gives the same person a different openid in each scope.

These commands are handled here and never reach Bub or the model, so the
codes never land in a prompt or on the tape.
"""

from __future__ import annotations

import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from loguru import logger

from .guard import Requester
from .guard import admin_entries
from .guard import is_admin
from .guard import set_registered_admins
from .security import command_prefix
from .security import parse_id_list
from .security import strip_command

if TYPE_CHECKING:
    from .config import QQConfig
    from .store import QQPlatformStore

BOOTSTRAP_TTL_SECONDS = 600.0
# Short: the code is posted in the group, where others could replay it first.
GROUP_CODE_TTL_SECONDS = 60.0
MAX_FAILURES = 5
LOCKOUT_SECONDS = 600.0
CODE_LENGTH = 10
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I

CLAIM_COMMAND = "qq.claim"
ADMINS_COMMAND = "qq.admins"


def is_admin_command(text: str) -> bool:
    """Whether ``text`` is a claim/admins command, which anyone may send."""

    body = strip_command(text)
    words = body.split() if body is not None else []
    return bool(words) and words[0] in (CLAIM_COMMAND, ADMINS_COMMAND)


@dataclass(frozen=True)
class _PairingCode:
    kind: Literal["bootstrap", "group"]
    expires_at: float
    issued_by: str = ""


def _new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


class AdminRegistry:
    """Pairing codes, failed-attempt lockout and the registered admin list."""

    def __init__(
        self,
        store: QQPlatformStore,
        config: QQConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock
        self._codes: dict[str, _PairingCode] = {}
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        # (c2c openid, text) for admins to tell privately; the channel sends them.
        self.notices: list[tuple[str, str]] = []
        self._publish()

    def _publish(self) -> None:
        set_registered_admins(self._store.admins())

    def has_admins(self) -> bool:
        return bool(admin_entries(self._config))

    def bootstrap_code(self) -> str | None:
        """The current bootstrap code (logged when minted); None once claimed."""

        if self.has_admins():
            return None
        now = self._clock()
        for code, entry in self._codes.items():
            if entry.kind == "bootstrap" and entry.expires_at > now:
                return code
        code = _new_code()
        self._codes[code] = _PairingCode("bootstrap", now + BOOTSTRAP_TTL_SECONDS)
        minutes = int(BOOTSTRAP_TTL_SECONDS // 60)
        logger.warning(
            "qq.admin.unclaimed no admins configured; send '{}{} {}' to the bot"
            " in a private (C2C) chat within {} minutes to become its admin",
            command_prefix(),
            CLAIM_COMMAND,
            code,
            minutes,
        )
        return code

    def handle(self, text: str, requester: Requester) -> str | None:
        """Reply for a claim/admins command, or None when ``text`` is not one.

        An empty reply means the command was handled and gets no answer.
        """

        body = strip_command(text)
        words = body.split() if body is not None else []
        if not words:
            return None
        if words[0] == CLAIM_COMMAND:
            return self._claim(words[1:], requester)
        if words[0] == ADMINS_COMMAND and is_admin(self._config, requester):
            return self._admins(words[1:], requester)
        return None

    # --- ,qq.claim ---------------------------------------------------------

    def _claim(self, args: list[str], requester: Requester) -> str:
        if not args:
            claim = f"{command_prefix()}{CLAIM_COMMAND}"
            return f"用法：{claim} <配对码>；管理员在单聊中发送 {claim} group 获取群登记码。"
        if args[0].lower() == "group":
            return self._issue_group_code(requester)
        identity = requester.identity
        now = self._clock()
        if self._locked_until.get(identity, 0.0) > now:
            # Already told once; answering every attempt would let anyone
            # make the bot flood the chat.
            logger.info("qq.admin.claim_ignored identity={} reason=locked", identity)
            return ""
        code = args[0].upper()
        entry = self._codes.get(code)
        if entry is None or entry.expires_at <= now:
            self._codes.pop(code, None)
            locked = self._record_failure(identity, now)
            if not self.has_admins():
                self.bootstrap_code()  # make sure a fresh code is in the log
            if locked:
                return "尝试次数过多，请 10 分钟后再试。"
            return "配对码无效或已过期。"
        if entry.kind == "bootstrap" and requester.scope != "c2c":
            # The code is now visible to the whole group: burn it.
            del self._codes[code]
            logger.warning("qq.admin.claim_code_exposed scope=group; minting a new code")
            self.bootstrap_code()
            return "配对码只能在单聊中使用。它已作废，请到服务器日志查看新的配对码。"
        if entry.kind == "group" and requester.scope != "group":
            return "这是群登记码，请在目标群里发送。"
        del self._codes[code]
        self._failures.pop(identity, None)
        self._store.add_admin(
            identity,
            registered_at=datetime.now(UTC).isoformat(timespec="seconds"),
            via=entry.kind,
            issued_by=entry.issued_by,
        )
        self._publish()
        logger.info(
            "qq.admin.registered identity={} via={} issued_by={}",
            identity,
            entry.kind,
            entry.issued_by or "-",
        )
        if entry.kind == "group" and entry.issued_by.startswith("c2c:"):
            # The code was visible in the group; let whoever asked for it
            # check that the registration was really theirs.
            self.notices.append(
                (
                    entry.issued_by.removeprefix("c2c:"),
                    (
                        f"你申请的群登记码已被使用：{identity} 已登记为管理员。"
                        f"如果不是你本人，请发送 {command_prefix()}{ADMINS_COMMAND} remove {identity}"
                    ),
                )
            )
        return f"已登记为管理员：{identity}"

    def _issue_group_code(self, requester: Requester) -> str:
        if requester.scope != "c2c":
            return f"请在与机器人的单聊中发送 {command_prefix()}{CLAIM_COMMAND} group。"
        if not is_admin(self._config, requester):
            return "只有管理员可以生成群登记码。"
        code = _new_code()
        self._codes[code] = _PairingCode(
            "group", self._clock() + GROUP_CODE_TTL_SECONDS, issued_by=requester.identity
        )
        minutes = int(GROUP_CODE_TTL_SECONDS // 60)
        return (
            f"群登记码：{code}\n在目标群里发送 {command_prefix()}{CLAIM_COMMAND} {code}"
            f"（{minutes} 分钟内有效，只能使用一次）。"
        )

    def _record_failure(self, identity: str, now: float) -> bool:
        """Count a failed attempt; True when it locks the sender out."""

        window = self._failures.setdefault(identity, deque())
        while window and now - window[0] > LOCKOUT_SECONDS:
            window.popleft()
        window.append(now)
        if len(window) >= MAX_FAILURES:
            self._locked_until[identity] = now + LOCKOUT_SECONDS
            window.clear()
            logger.warning("qq.admin.claim_locked identity={}", identity)
            return True
        return False

    # --- ,qq.admins --------------------------------------------------------

    def _admins(self, args: list[str], requester: Requester) -> str:
        configured = parse_id_list(self._config.admin_users)
        registered = self._store.admins()
        if len(args) >= 2 and args[0] == "remove":
            target = args[1]
            if target in configured:
                return f"{target} 写在配置 admin_users 中，请修改配置后重启。"
            if not self._store.remove_admin(target):
                return f"没有找到已登记的管理员 {target}。"
            self._publish()
            logger.info(
                "qq.admin.removed identity={} by={}", target, requester.identity
            )
            if not self.has_admins():
                self.bootstrap_code()
            return f"已移除管理员 {target}。"
        lines = ["管理员："]
        lines += [f"- {entry}（配置）" for entry in sorted(configured)]
        lines += [f"- {entry}（已登记）" for entry in sorted(registered - configured)]
        lines.append(f"移除已登记的管理员：{command_prefix()}{ADMINS_COMMAND} remove <身份>")
        return "\n".join(lines)
