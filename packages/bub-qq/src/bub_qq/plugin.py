import json
import sys
import time
from collections.abc import Mapping
from typing import Any

import bub
from bub import hookimpl
from bub import inquirer as bub_inquirer
from bub.channels import Channel
from bub.channels.contracts import MessageHandler
from bub.core.tools import Tool
from bub.envelope import field_of
from bub.hooks.interception import LLMCallDecision
from bub.hooks.interception import LLMCallRequest
from bub.hooks.interception import LLMCallResult
from bub.hooks.interception import ToolCall
from bub.hooks.interception import ToolCallDecision
from bub.hooks.interception import ToolCallResult
from bub.turn import TurnState
from loguru import logger

from .bub_version import check_bub_version

# Before importing modules that use newer Bub interfaces, so an old Bub
# fails to load the plugin with the reason.
check_bub_version(bub.__version__)

from . import tools as qq_tools
from .config import QQConfig
from .security import QQ_CONTEXT_KEY
from .security import QQ_STATE_KEY
from .security import SlidingWindowRateLimiter
from .session import BoundedDict
from .approval import ApprovalOutcome
from .approval import consume_token
from .approval import record_outcome
from .approval import request_approval
from .approval import send_notice
from .guard import Decision
from .guard import Requester
from .guard import classify
from .guard import evaluate
from .netguard import web_fetch_for_call
from .store import resolve_state_path
from .workspace import artifact_root
from .workspace import workspace_from_state

CHANNEL_NAME = "qq"
RECEIVE_MODES = ["webhook", "websocket"]

# Bub's builtin <response_instruct> assumes skill-based channels ("your
# plain reply will be ignored"). QQ works differently in each reply mode,
# so spell the actual contract out; the response criteria referenced below
# are the numbered conditions in that builtin block.
DIRECT_REPLY_PROMPT = """\
<qq_response_instruct>
This conversation is on the QQ channel ($qq), which overrides the generic channel guidance above:
- There is no QQ send skill or tool to call. Your plain final reply text IS delivered to the QQ chat as-is.
- To reply, write the reply text and end the turn.
- If no response is needed (apply the response criteria above), output exactly <no_reply/> and nothing else; the channel swallows it and nothing is sent.
</qq_response_instruct>"""

TOOL_REPLY_PROMPT = """\
<qq_response_instruct>
This conversation is on the QQ channel ($qq), which overrides the generic channel guidance above:
- To reply, call the qq.send tool with the message text or media_url / media_path for native media. The plugin downloads media_url itself and uploads the file; do not bash/curl it. To @ a group member, pass at_user_ids with their sender_id. Reply targeting (msg_id/msg_seq) is handled internally; never construct protocol fields yourself.
- Your plain final reply text is NOT delivered to the chat.
- If no response is needed, do not call qq.send. After qq.send succeeds, stop; do not qq.send <no_reply/>.
</qq_response_instruct>"""

_rate_limiter: SlidingWindowRateLimiter | None = None
_rate_limit = 0
# When each rate-limited sender was last told so (tool mode), to notify
# at most once per window instead of on every blocked turn.
_rate_notified: BoundedDict[str, float] = BoundedDict(1024)
_RATE_VERDICT_KEY = "rate_limit_allowed"


def _qq_state(state: TurnState) -> dict[str, Any] | None:
    if not isinstance(state, dict):
        return None
    qq_state = state.get(QQ_STATE_KEY)
    return qq_state if isinstance(qq_state, dict) else None


def _turn_declined_reply(qq_state: dict[str, Any], result: LLMCallResult) -> bool:
    """Whether this LLM result ends the turn with the model declining to reply.

    In tool reply mode the agent loop stops at the first LLM response
    without tool calls. If no qq.send call was recorded earlier in the
    turn (``replied_via_tool``), that final response means the model chose
    silence — the only turn outcome that otherwise leaves no log trace.
    """

    if result.error is not None or result.tool_calls:
        return False
    return not qq_state.get("replied_via_tool")


def _get_rate_limiter(config: QQConfig) -> SlidingWindowRateLimiter | None:
    if config.llm_rate_limit_per_minute <= 0:
        return None
    global _rate_limiter, _rate_limit
    if _rate_limiter is None or _rate_limit != config.llm_rate_limit_per_minute:
        _rate_limiter = SlidingWindowRateLimiter(
            max_calls=config.llm_rate_limit_per_minute,
            window_seconds=60.0,
        )
        _rate_limit = config.llm_rate_limit_per_minute
    return _rate_limiter


def _channel_enabled(current_config: dict[str, Any]) -> bool:
    enabled_channels = current_config.get("enabled_channels")
    if not isinstance(enabled_channels, str):
        return True
    value = enabled_channels.strip()
    if not value or value.lower() == "all":
        return True
    return CHANNEL_NAME in {item.strip() for item in value.split(",") if item.strip()}


@hookimpl
def provide_tools() -> list[Tool]:
    return list(qq_tools.TOOLS)


@hookimpl
def provide_channels(message_handler: MessageHandler) -> list[Channel]:
    from .channel import QQChannel

    return [QQChannel(message_handler)]


@hookimpl
def load_state(message: Any, session_id: str) -> TurnState | None:
    """Copy QQ metadata from the inbound message into the turn state.

    Inbound adaptation stores scope/sender/role details on
    ``ChannelMessage.context``; exposing them under ``state["qq"]`` lets the
    interception hooks below enforce per-sender policies.
    """

    context = field_of(message, "context")
    if not isinstance(context, dict):
        return None
    qq_context = context.get(QQ_CONTEXT_KEY)
    if not isinstance(qq_context, dict):
        return None
    state = {**qq_context, "session_id": session_id}
    if field_of(message, "kind") != "command":
        state["batch_senders"] = _batch_senders(field_of(message, "content"))
    return {QQ_STATE_KEY: state}


# Stands for a merged line that names no sender; never an admin identity.
UNKNOWN_SENDER = "?"


def _batch_senders(content: object) -> list[str]:
    """Every sender in a turn's content, which Bub may merge from several messages.

    Each inbound message is one JSON line (``json.dumps`` escapes newlines in
    the text), and a merged batch joins them with newlines. A line that is
    not such a payload counts as an unknown sender.
    """

    if not isinstance(content, str):
        return [UNKNOWN_SENDER]
    senders: list[str] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            payload = None
        sender = payload.get("sender_id") if isinstance(payload, dict) else None
        name = sender if isinstance(sender, str) and sender else UNKNOWN_SENDER
        if name not in senders:
            senders.append(name)
    return senders


@hookimpl
def system_prompt(prompt: Any, state: TurnState) -> str | None:
    """Describe the QQ reply contract for the active reply mode."""

    del prompt
    qq_state = _qq_state(state)
    if qq_state is None:
        return None
    config = bub.ensure_config(QQConfig)
    workspace = workspace_from_state(state)
    root = artifact_root(workspace)
    jail = (
        f"<qq_workspace>\nWorking directory: {workspace}\n"
        "- File tools only work inside this directory; .env, .git/ and QQ state"
        " files are always refused.\n"
        "- Attachments are not downloaded automatically. When you need one, call"
        " qq.fetch_attachment with its message_id and position; it is saved under"
        f" {root / 'inbox'}/.\n"
        f"- qq.send media_path only accepts files under {root / 'outbox'}/ or"
        f" {root / 'inbox'}/.\n"
        "- Shell and file writes are admin-only; an admin's shell command waits for"
        " an approval tap and its result is posted to the chat, not returned to you.\n"
        "</qq_workspace>"
    )
    if config.reply_mode == "tool":
        return f"{TOOL_REPLY_PROMPT}\n{jail}"
    return f"{DIRECT_REPLY_PROMPT}\n{jail}"


@hookimpl
async def before_llm_call(
    request: LLMCallRequest, state: TurnState
) -> LLMCallRequest | LLMCallDecision | None:
    del request
    qq_state = _qq_state(state)
    if qq_state is None:
        return None
    config = bub.ensure_config(QQConfig)
    limiter = _get_rate_limiter(config)
    if limiter is None:
        return None
    key = f"{qq_state.get('session_id')}|{qq_state.get('sender_id')}"
    # Count each turn once: a turn makes one LLM call per tool step, and
    # cutting it off midway would leave a half-done answer.
    allowed = qq_state.get(_RATE_VERDICT_KEY)
    if allowed is None:
        allowed = limiter.allow(key)
        qq_state[_RATE_VERDICT_KEY] = allowed
    if allowed:
        return None
    logger.warning(
        "qq.security.llm_rate_limited session_id={} sender_id={}",
        qq_state.get("session_id"),
        qq_state.get("sender_id"),
    )
    if config.reply_mode == "tool":
        # Tool mode drops direct output, so the finish text below would
        # never reach the chat; send the notice ourselves.
        await _notify_rate_limited(qq_state, config, key)
    return LLMCallDecision.finish(config.llm_rate_limit_notice)


async def _notify_rate_limited(
    qq_state: dict[str, Any], config: QQConfig, key: str
) -> None:
    now = time.monotonic()
    last = _rate_notified.get(key)
    if last is not None and now - last < 60.0:
        return
    _rate_notified[key] = now
    session_id = str(qq_state.get("session_id") or "")
    if str(qq_state.get("scope") or "") == "group":
        chat_id = f"group:{qq_state.get('group_openid') or ''}"
    else:
        chat_id = f"c2c:{qq_state.get('sender_id') or ''}"
    try:
        await send_notice(session_id, chat_id, config.llm_rate_limit_notice)
    except Exception as exc:
        logger.warning("qq.security.rate_notice_failed error={}", exc)


@hookimpl
async def before_tool_call(
    call: ToolCall, state: TurnState
) -> ToolCallDecision | None:
    qq_state = _qq_state(state)
    if qq_state is None:
        qq_state = _unattributed_qq_state(state)
        if qq_state is None:
            return None
        logger.warning(
            "qq.security.unattributed_turn tool={} session_id={} reason=no QQ sender;"
            " checking as an unknown member",
            call.tool,
            qq_state["session_id"],
        )
    try:
        return await _check_tool_call(call, state, qq_state)
    except Exception:
        # Bub logs a failing before_tool_call and runs the tool anyway, so
        # an error in the Guard must deny here rather than propagate.
        logger.exception(
            "qq.security.guard_error tool={} session_id={}",
            call.tool,
            qq_state.get("session_id"),
        )
        return ToolCallDecision.deny(
            "The QQ permission check failed, so this call was blocked."
        )


def _unattributed_qq_state(state: TurnState) -> dict[str, Any] | None:
    """Guard state for a QQ session turn that carries no QQ sender.

    Turns started inside Bub (a scheduled job, another plugin) reach a QQ
    session without the inbound QQ metadata. They are still QQ chats, so
    the Guard checks them as an unknown member: no sender id, hence never an
    admin, even in a private chat whose session id names the user.
    """

    if not isinstance(state, dict):
        return None
    session_id = str(state.get("session_id") or "")
    parts = session_id.split(":", 2)
    if len(parts) < 3 or parts[0] != CHANNEL_NAME:
        return None
    scope, chat = parts[1], parts[2]
    if scope == "group":
        return {"scope": "group", "group_openid": chat, "sender_id": "", "session_id": session_id}
    return {"scope": "c2c", "sender_id": "", "session_id": session_id}


async def _check_tool_call(
    call: ToolCall, state: TurnState, qq_state: dict[str, Any]
) -> ToolCallDecision | None:
    config = bub.ensure_config(QQConfig)
    requester = Requester.from_state(qq_state)
    session_id = str(qq_state.get("session_id") or "")
    workspace = workspace_from_state(state)
    decision = evaluate(
        call,
        requester,
        config=config,
        workspace=workspace,
        protected=(resolve_state_path(config),),
    )
    command = _is_command(call)
    if decision.action == "approval":
        if command and consume_token(session_id, requester, call):
            return None
        if command and qq_state.get("approval_id"):
            # An approved command whose token no longer matches: refuse it
            # rather than asking again.
            qq_state["approval_status"] = "refused"
            return ToolCallDecision.deny("命令未执行：审批已失效。")
        tools = _agent_tools(state)
        if tools is None:
            return ToolCallDecision.deny("This call needs admin approval, which is unavailable here.")
        # Once an admin allows it, the call runs as a comma command in this
        # session; the model gets the pending notice now.
        message = await request_approval(
            call,
            requester,
            config=config,
            session_id=session_id,
            tools=tools,
            requester_name=str(qq_state.get("sender_name") or "").strip(),
        )
        return ToolCallDecision.replace(message)
    if decision.allowed and decision.resource == "fetch":
        return await _guarded_web_fetch(call)
    if decision.allowed:
        return None
    if command:
        if qq_state.get("approval_id"):
            qq_state["approval_status"] = "refused"
        logger.warning(
            "qq.command.denied session_id={} requester={} tool={} reason={}",
            session_id,
            requester.identity,
            call.tool,
            decision.reason,
        )
        return ToolCallDecision.deny(f"命令未执行：{decision.reason}")
    logger.warning(
        "qq.security.tool_denied tool={} resource={} session_id={} requester={} reason={}",
        call.tool,
        decision.resource,
        session_id,
        requester.identity,
        decision.reason,
    )
    return ToolCallDecision.deny(_model_facing_denial(decision))


def _is_command(call: ToolCall) -> bool:
    """Whether Bub runs ``call`` for a comma command rather than for the model."""

    return call.context is not None and call.context.command


def _agent_tools(state: TurnState) -> Mapping[str, Tool] | None:
    """The tools of the agent running this turn, which an approved command runs with."""

    agent = state.get("_runtime_agent") if isinstance(state, dict) else None
    tools = getattr(agent, "tools", None)
    return tools if isinstance(tools, Mapping) else None


_PERMISSION_GUIDANCE = (
    " This is a permission rule for this chat, not a temporary error: do not"
    " retry this call or work around it with other tools. Tell the user it was"
    " refused and why; an admin has to run or approve it."
)


def _model_facing_denial(decision: Decision) -> str:
    """The denial text for the model, telling it whether retrying can help.

    Without this the model tends to retry a refused shell call over and over.
    """

    if decision.fixable:
        return decision.reason
    return decision.reason + _PERMISSION_GUIDANCE


async def _guarded_web_fetch(call: ToolCall) -> ToolCallDecision:
    """Run ``web.fetch`` ourselves, restricted to public addresses."""

    ok, text = await web_fetch_for_call(call.arguments)
    if ok:
        return ToolCallDecision.replace(text)
    logger.warning("qq.security.fetch_denied tool={} reason={}", call.tool, text)
    return ToolCallDecision.deny(text)


@hookimpl
def after_llm_call(
    request: LLMCallRequest, result: LLMCallResult, state: TurnState
) -> None:
    qq_state = _qq_state(state)
    if qq_state is None:
        return
    logger.info(
        # Bub's hook only reports the requested model; with fallback_models the
        # answering one can differ (see the trace's chat span).
        "qq.audit.llm session_id={} sender_id={} requested_model={} duration_ms={} error={}",
        qq_state.get("session_id"),
        qq_state.get("sender_id"),
        request.model,
        result.duration_ms,
        type(result.error).__name__ if result.error is not None else "",
    )
    config = bub.ensure_config(QQConfig)
    if config.reply_mode == "tool" and _turn_declined_reply(qq_state, result):
        logger.info(
            "qq.reply declined session_id={} sender_id={} reason=no_send_tool_call",
            qq_state.get("session_id"),
            qq_state.get("sender_id"),
        )


@hookimpl
def save_state(session_id: str, state: TurnState, message: Any, model_output: str) -> None:
    """Remember what an approved command printed, for the model's next turn."""

    qq_state = _qq_state(state)
    if qq_state is None or not qq_state.get("approval_id"):
        return
    # Bub calls save_state in a finally block: a failed command (non-zero
    # exit raises in Bub's bash) is the exception in flight, and the output
    # is in its message, while model_output is empty.
    error = sys.exc_info()[1]
    approval_id = str(qq_state["approval_id"])
    command = str(qq_state.get("approval_preview") or "")
    if qq_state.get("approval_status") == "refused":
        # The Guard refused the approved command when it ran.
        outcome = ApprovalOutcome(id=approval_id, command=command, status="refused")
    else:
        outcome = ApprovalOutcome(
            id=approval_id,
            command=command,
            status="failed" if error is not None else "ran",
            output=str(error) if error is not None else (model_output or "(no output)"),
        )
    record_outcome(session_id, outcome)


@hookimpl
def after_tool_call(call: ToolCall, result: ToolCallResult, state: TurnState) -> None:
    qq_state = _qq_state(state)
    if qq_state is None:
        return
    if classify(call) == "reply":
        # Any qq.send attempt means the model did not decline this turn;
        # after_llm_call uses this to log genuine silence.
        qq_state["replied_via_tool"] = True
    logger.info(
        "qq.audit.tool session_id={} sender_id={} role={} tool={} duration_ms={} error={}",
        qq_state.get("session_id"),
        qq_state.get("sender_id"),
        qq_state.get("sender_role"),
        call.tool,
        result.duration_ms,
        type(result.error).__name__ if result.error is not None else "",
    )


@hookimpl
def onboard_config(current_config: dict[str, Any]) -> dict[str, Any] | None:
    if not _channel_enabled(current_config):
        return None

    current = current_config.get(CHANNEL_NAME)
    config = current if isinstance(current, dict) else {}
    receive_mode_default = str(config.get("receive_mode") or "webhook")
    if receive_mode_default not in RECEIVE_MODES:
        receive_mode_default = "webhook"

    return {
        CHANNEL_NAME: {
            "appid": bub_inquirer.ask_text(
                "QQ app ID",
                default=str(config.get("appid") or ""),
            ),
            "secret": bub_inquirer.ask_secret("QQ secret")
            or str(config.get("secret") or ""),
            "receive_mode": bub_inquirer.ask_select(
                "QQ receive mode",
                choices=RECEIVE_MODES,
                default=receive_mode_default,
            ),
        }
    }
