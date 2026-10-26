"""Admin approval for calls the Guard marks ``approval`` (fixed keyboard).

Flow: the Guard returns ``approval`` → :func:`request_approval` posts a card
showing the full call and remembers it → an admin taps a button →
:func:`begin_approval_click` checks the operator is an admin → on "allow"
:func:`complete_approval_click` issues a one-time token and hands the call
back to Bub as a comma command in the original session, so it runs in that
session's execution environment and lands on its tape. The call is first
rewritten as the equivalent command line (:func:`call_to_command_line`).
Bub runs commands through ``before_tool_call``, so running re-enters the
Guard, which accepts the token only for the exact session, requester, tool
and arguments that were shown on the card. There is no always-allow: every
shell command is confirmed on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from bub.channels.message import ChannelMessage
from bub.hooks.interception import ToolCall
from loguru import logger

from .guard import Requester
from .guard import canonical_tool
from .guard import group_admin_member_ids
from .guard import is_admin
from .runtime import get_active_channel
from .security import command_prefix
from .security import strip_command
from .session import BoundedDict

if TYPE_CHECKING:
    from bub.core.tools import Tool

    from .config import QQConfig

APPROVAL_TTL_SECONDS = 240.0
PREVIEW_LIMIT = 1500
_MAX_PENDING = 64
_MAX_TOKENS = 256

ACK_OK = 0
ACK_FAILED = 1
ACK_NO_PERMISSION = 4

_clock: Callable[[], float] = time.monotonic

# Outcomes waiting to ride along on the session's next message to the model.
_OUTCOMES_PER_MESSAGE = 3
_OUTCOME_OUTPUT_LIMIT = 500
_OUTCOMES_KEPT = 20


@dataclass
class PendingApproval:
    id: str
    tool: str
    arguments: dict[str, Any]
    requester: Requester
    session_id: str
    chat_id: str
    preview: str
    created_at: float
    requester_name: str = ""
    command_line: str = ""

    @property
    def requester_id(self) -> str:
        return self.requester.sender_id

    @property
    def group_openid(self) -> str:
        return self.requester.group_openid


_pending: BoundedDict[str, PendingApproval] = BoundedDict(_MAX_PENDING)
_tokens: BoundedDict[tuple[str, str, str, str], float] = BoundedDict(_MAX_TOKENS)


@dataclass(frozen=True)
class ApprovalOutcome:
    """How one approval ended, for the model to learn on its next turn.

    An approved command runs through Bub's command path, whose output never
    enters the model's context, so the model would keep thinking it pends.
    """

    id: str
    command: str
    status: Literal["ran", "denied", "expired", "refused", "failed"]
    output: str = ""


_outcomes: BoundedDict[str, list[ApprovalOutcome]] = BoundedDict(_MAX_PENDING)


def record_outcome(session_id: str, outcome: ApprovalOutcome) -> None:
    if len(outcome.output) > _OUTCOME_OUTPUT_LIMIT:
        outcome = replace(outcome, output=outcome.output[:_OUTCOME_OUTPUT_LIMIT] + "…")
    queued = _outcomes.get(session_id, [])
    _outcomes[session_id] = [*queued, outcome][-_OUTCOMES_KEPT:]


def _expire_pending(session_id: str) -> None:
    now = _clock()
    for pending in list(_pending.values()):
        if pending.session_id == session_id and now - pending.created_at > APPROVAL_TTL_SECONDS:
            _pending.pop(pending.id, None)
            record_outcome(
                session_id, ApprovalOutcome(id=pending.id, command=pending.preview, status="expired")
            )


def attach_approval_outcomes(message: ChannelMessage) -> ChannelMessage:
    """Add ended approvals to a message the model will read, once.

    The outcomes are appended to the user message rather than the system
    prompt, so they do not invalidate the prompt cache and stay on the tape.
    """

    if not message.is_active:
        return message
    _expire_pending(message.session_id)
    if not _outcomes.get(message.session_id):
        return message
    try:
        payload = json.loads(message.content)
    except ValueError:
        return message
    if not isinstance(payload, dict):
        return message
    queued = _outcomes.pop(message.session_id)
    recent = queued[-_OUTCOMES_PER_MESSAGE:]
    payload["approval_results"] = [asdict(outcome) for outcome in recent]
    if len(queued) > len(recent):
        payload["approval_results_omitted"] = len(queued) - len(recent)
    return replace(message, content=json.dumps(payload, ensure_ascii=False))


def reset_approval_state() -> None:
    _outcomes.clear()
    _pending.clear()
    _tokens.clear()


def call_digest(tool: str, arguments: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"tool": canonical_tool(tool), "arguments": arguments or {}},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _token_key(
    session_id: str, requester: Requester, call: ToolCall
) -> tuple[str, str, str, str]:
    return (
        session_id,
        requester.identity,
        canonical_tool(call.tool),
        call_digest(call.tool, call.arguments),
    )


def issue_token(pending: PendingApproval) -> None:
    call = ToolCall(run_id="approval", tool=pending.tool, arguments=pending.arguments)
    _tokens[_token_key(pending.session_id, pending.requester, call)] = (
        _clock() + APPROVAL_TTL_SECONDS
    )


def consume_token(session_id: str, requester: Requester, call: ToolCall) -> bool:
    """Use up the token for exactly this call; False if absent or expired."""

    expires_at = _tokens.pop(_token_key(session_id, requester, call), None)
    return expires_at is not None and _clock() <= expires_at


def comma_command_to_call(line: str, tools: Mapping[str, Tool]) -> ToolCall:
    """Map a comma command to exactly the call Bub's ``Agent.run_command`` makes.

    ``tools`` are the tools of the agent that runs the command. Bub looks the
    first word up in them as typed (no aliasing) and runs anything else as
    ``bash`` with the whole line. Positional words are bound to the tool's
    parameters in order, so the Guard sees e.g. ``,fs.read .env`` as
    ``fs.read(path=".env")``.
    """

    from bub.core.commands import parse_command, parse_command_args

    body = strip_command(line)
    if body is None:
        body = line.strip()
    if not body:
        raise ValueError("empty command")
    name, tokens = parse_command(body)
    tool = tools.get(name)
    if tool is None:
        return ToolCall(run_id="command", tool="bash", arguments={"command": body})
    arguments = parse_command_args(tokens).bind(list((tool.parameters or {}).get("properties", {})))
    return ToolCall(run_id="command", tool=name, arguments=arguments)


def call_to_command_line(tool: str, arguments: dict[str, Any]) -> str | None:
    """The comma command Bub would run as ``tool(**arguments)``, or None.

    Only scalar arguments survive Bub's ``key=value`` command parsing, so a
    call with list or dict arguments cannot be handed back this way.
    """

    words = [canonical_tool(tool)]
    for key, value in arguments.items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, str | int | float):
            text = str(value)
        else:
            return None
        words.append(f"{key}={shlex.quote(text)}")
    return command_prefix() + " ".join(words)


def preview_call(tool: str, arguments: dict[str, Any]) -> str:
    """The call as an admin should read it on the card: the command first.

    Other scalar arguments follow on a comment line, so a shell command never
    hides inside JSON. Anything else is shown as JSON.
    """

    command = arguments.get("command")
    others = {key: value for key, value in arguments.items() if key != "command" and value is not None}
    if isinstance(command, str) and all(isinstance(v, str | int | float | bool) for v in others.values()):
        if not others:
            return command
        notes = " ".join(f"{key}={value}" for key, value in others.items())
        return f"{command}\n# {notes}"
    return json.dumps(arguments, ensure_ascii=False, default=str, indent=2)


# Shell patterns worth a second look before approving. A hint for the human
# on the card, not a filter: approval stays the decision.
_RISKY_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\brm\s+(?:-\S*\s+)*-\S*[rRf]"), "递归或强制删除（rm -r / -f）"),
    (re.compile(r"\bfind\b[^\n]*\s-delete\b"), "批量删除（find -delete）"),
    (re.compile(r"\bmkfs\b|\bdd\b[^\n]*\bof="), "格式化或覆盖磁盘"),
    (re.compile(r"\b(?:chmod|chown)\s+-\S*R"), "递归修改权限或属主"),
    (re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"), "下载并直接执行脚本"),
    (re.compile(r"\bgit\s+(?:push\b[^\n]*--force|reset\s+--hard|clean\s+-\S*f)"), "不可撤销的 git 操作"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"), "关机或重启"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork 炸弹"),
)


# Not destructive, but worth a look: these reach the network without the
# plugin's address check (a refused qq.view_image / media_url retried by hand).
_NOTABLE_COMMANDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:curl|wget|aria2c)\b[^\n]*\bhttps?://"), "下载网络内容，不经过插件的内网地址检查"),
)


def risky_command_reasons(preview: str) -> list[str]:
    return [reason for pattern, reason in _RISKY_COMMANDS if pattern.search(preview)]


def notable_command_reasons(preview: str) -> list[str]:
    return [reason for pattern, reason in _NOTABLE_COMMANDS if pattern.search(preview)]


def _fenced(text: str, language: str = "") -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return f"{fence}{language}\n{text}\n{fence}"


def _preview_language(pending: PendingApproval) -> str:
    # QQ labels an unmarked block "text"; name it so admins see what they read.
    if pending.tool == "bash":
        return "bash"
    return "json" if pending.preview.lstrip().startswith("{") else ""


def build_approval_keyboard(
    approval_id: str, *, approver_ids: list[str] | None = None
) -> dict[str, Any]:
    if approver_ids:
        permission: dict[str, Any] = {"type": 0, "specify_user_ids": list(approver_ids)}
    else:
        permission = {"type": 2}

    def button(button_id: str, label: str, visited: str, data: str, style: int) -> dict:
        return {
            "id": button_id,
            "render_data": {"label": label, "visited_label": visited, "style": style},
            "action": {
                "type": 1,
                "data": data,
                "permission": dict(permission),
                "unsupport_tips": "请升级 QQ 后审批",
            },
            "group_id": "approval",
        }

    return {
        "content": {
            "rows": [
                {
                    "buttons": [
                        button("allow", "✅ 允许一次", "已允许", f"approve:{approval_id}:allow", 1),
                        button("deny", "❌ 拒绝", "已拒绝", f"approve:{approval_id}:deny", 0),
                    ]
                }
            ]
        }
    }


def parse_approval_button(button_data: str) -> tuple[str, str] | None:
    parts = button_data.split(":")
    if len(parts) != 3 or parts[0] != "approve":
        return None
    _, approval_id, decision = parts
    if decision not in {"allow", "deny"}:
        return None
    return approval_id, decision


async def request_approval(
    call: ToolCall,
    requester: Requester,
    *,
    config: QQConfig,
    session_id: str,
    tools: Mapping[str, Tool],
    requester_name: str = "",
) -> str:
    """Post an approval card; return the text the caller reports back.

    The call is rewritten as a command line, so an approval always runs
    through Bub's command path; ``tools`` are the tools of the agent that
    will run it.
    """

    arguments = dict(call.arguments or {})
    preview = preview_call(call.tool, arguments)
    if len(preview) > PREVIEW_LIMIT:
        logger.warning(
            "qq.approval.too_long tool={} session_id={} length={}",
            call.tool,
            session_id,
            len(preview),
        )
        return (
            f"Not run: the command is longer than {PREVIEW_LIMIT} characters and"
            " cannot be shown in full for approval. Split it into shorter steps."
        )
    command_line = call_to_command_line(call.tool, arguments)
    if command_line is None:
        return "Not run: this call has list or dict arguments and cannot be approved."
    # The token must match the call Bub's command path will make.
    call = comma_command_to_call(command_line, tools)
    arguments = dict(call.arguments)
    chat_id = (
        f"group:{requester.group_openid}"
        if requester.scope == "group"
        else f"c2c:{requester.sender_id}"
    )
    pending = PendingApproval(
        id=uuid.uuid4().hex[:12],
        tool=canonical_tool(call.tool),
        arguments=arguments,
        requester=requester,
        session_id=session_id,
        chat_id=chat_id,
        preview=preview,
        created_at=_clock(),
        requester_name=requester_name,
        command_line=command_line,
    )
    _pending[pending.id] = pending
    try:
        await _send_approval_message(pending, config)
    except Exception as exc:
        logger.warning("qq.approval.send_failed id={} error={}", pending.id, exc)
        _pending.pop(pending.id, None)
        return "Not run: the approval card could not be sent."
    logger.info(
        "qq.approval.requested id={} tool={} session_id={} requester={}",
        pending.id,
        pending.tool,
        session_id,
        requester.identity,
    )
    minutes = int(APPROVAL_TTL_SECONDS // 60)
    return (
        f"已提交管理员审批（{pending.id}），{minutes} 分钟内有效。"
        "管理员批准后命令会执行，结果会发到聊天中。"
    )


async def _send_approval_message(pending: PendingApproval, config: QQConfig) -> None:
    from .outbound.media import build_outbound_context
    from .outbound.send_flow import is_delivered_send_result

    channel = get_active_channel()
    if channel is None:
        raise RuntimeError("QQ channel is not running")
    approver_ids = (
        group_admin_member_ids(config, pending.group_openid)
        if pending.requester.scope == "group"
        else None
    )
    minutes = int(APPROVAL_TTL_SECONDS // 60)
    reasons = risky_command_reasons(pending.preview)
    warning = (
        f"⚠️ 高危命令：{'、'.join(reasons)}。执行后无法撤销，工作区内的文件可能被删除，请确认影响范围后再批准。\n\n"
        if reasons
        else ""
    )
    notes = notable_command_reasons(pending.preview)
    if notes:
        warning += f"ℹ️ 注意：{'、'.join(notes)}，请确认网址可信后再批准。\n\n"
    body = (
        "## 命令执行审批\n\n"
        f"{warning}"
        f"- 工具: `{pending.tool}`\n"
        f"- 请求人: {pending.requester_name or pending.requester_id}\n"
        f"- 有效期: {minutes} 分钟，仅管理员可批准\n\n"
        f"{_fenced(pending.preview, _preview_language(pending))}"
    )
    result = await channel.send_for_result(
        ChannelMessage(
            session_id=pending.session_id,
            channel=channel.name,
            chat_id=pending.chat_id,
            content=body,
            context=build_outbound_context(
                keyboard=build_approval_keyboard(pending.id, approver_ids=approver_ids)
            ),
        )
    )
    if not is_delivered_send_result(result):
        raise RuntimeError("approval message was not sent")


@dataclass(frozen=True)
class ApprovalClickPlan:
    """PUT ack first, then optionally run the pending call."""

    ack_code: int
    notice: str | None = None
    pending: PendingApproval | None = None
    decision: str | None = None


def begin_approval_click(
    *,
    approval_id: str,
    decision: str,
    operator: Requester,
    config: QQConfig,
) -> ApprovalClickPlan:
    pending = _pending.get(approval_id)
    if pending is None:
        return ApprovalClickPlan(ack_code=ACK_FAILED, notice="这条审批不存在或已经处理过。")
    if _clock() - pending.created_at > APPROVAL_TTL_SECONDS:
        _pending.pop(approval_id, None)
        record_outcome(
            pending.session_id,
            ApprovalOutcome(id=pending.id, command=pending.preview, status="expired"),
        )
        return ApprovalClickPlan(ack_code=ACK_FAILED, notice="审批已过期。")
    if not is_admin(config, operator):
        logger.warning(
            "qq.approval.unauthorized id={} operator={}", approval_id, operator.identity
        )
        return ApprovalClickPlan(ack_code=ACK_NO_PERMISSION, notice=None)
    _pending.pop(approval_id, None)
    return ApprovalClickPlan(ack_code=ACK_OK, pending=pending, decision=decision)


async def complete_approval_click(plan: ApprovalClickPlan) -> str | None:
    pending = plan.pending
    if pending is None or plan.decision is None:
        return plan.notice
    if plan.decision == "deny":
        logger.info("qq.approval.denied id={} tool={}", pending.id, pending.tool)
        record_outcome(
            pending.session_id,
            ApprovalOutcome(id=pending.id, command=pending.preview, status="denied"),
        )
        return f"已拒绝 `{pending.tool}`（{pending.id}）。"
    logger.info("qq.approval.allowed id={} tool={}", pending.id, pending.tool)
    issue_token(pending)
    output = await _dispatch_approved_command(pending)
    header = f"已允许 `{pending.tool}`（{pending.id}）。"
    return f"{header}\n\n{output}" if output else header


async def _dispatch_approved_command(pending: PendingApproval) -> str:
    channel = get_active_channel()
    dispatch = getattr(channel, "dispatch_approved_command", None)
    if dispatch is None:
        return "执行失败: QQ 渠道未运行。"
    try:
        await dispatch(pending)
    except Exception as exc:
        logger.warning("qq.approval.command_dispatch_failed id={} error={}", pending.id, exc)
        record_outcome(
            pending.session_id,
            ApprovalOutcome(id=pending.id, command=pending.preview, status="failed", output=str(exc)),
        )
        return f"执行失败: {exc}"
    return ""


async def send_notice(session_id: str, chat_id: str, text: str) -> None:
    channel = get_active_channel()
    if channel is None:
        return
    await channel.send_for_result(
        ChannelMessage(
            session_id=session_id,
            channel=channel.name,
            chat_id=chat_id,
            content=text,
        )
    )
