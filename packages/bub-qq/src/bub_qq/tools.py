"""Native tools and ops comma commands for the QQ channel.

``qq.send`` is the reply tool for ``reply_mode="tool"``: direct model
output is routed to the "null" channel, so calling the tool is the only
way a reply reaches the chat and *not* calling it is how the model stays
silent. It reuses the channel's send services, so passive
``msg_id``/``msg_seq`` targeting, dedupe and the active-message fallback
all behave exactly as in direct mode.

Tools registered with ``agent_use=False`` are ops comma commands: they
never appear in the model's tool list and can only be run as ``,name``
by senders who pass the comma-command gate (group owners/admins or
``admin_users``).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

import bub
from bub.channels.message import ChannelMessage
from bub.tools import ToolContext, tool

from .config import QQConfig
from .runtime import get_active_channel
from .security import QQ_STATE_KEY, REPLY_TOOL_NAME


@tool(name=REPLY_TOOL_NAME, context=True)
async def qq_send(content: str, *, context: ToolContext) -> str:
    """Send a message to the current QQ chat (group or private).

    Pass only the message text; reply targeting (msg_id/msg_seq) is
    handled by the channel. Call once per message you want delivered.
    To stay silent this turn, do not call this tool.
    """

    config = bub.ensure_config(QQConfig)
    if config.reply_mode != "tool":
        return (
            "Not sent: qq.send is disabled (qq.reply_mode is 'direct')."
            " Write your reply as plain text instead."
        )
    qq_state = context.state.get(QQ_STATE_KEY)
    if not isinstance(qq_state, dict):
        return "Not sent: qq.send is only available inside QQ channel sessions."
    channel = get_active_channel()
    if channel is None:
        return "Not sent: the QQ channel is not running in this process."

    session_id = str(qq_state.get("session_id") or "")
    if str(qq_state.get("scope") or "") == "group":
        chat_id = f"group:{qq_state.get('group_openid') or ''}"
    else:
        chat_id = f"c2c:{qq_state.get('sender_id') or ''}"

    result = await channel.send_for_result(
        ChannelMessage(
            session_id=session_id,
            channel=channel.name,
            chat_id=chat_id,
            content=content,
        )
    )
    if result is None:
        return (
            "Not sent: the QQ channel skipped or failed this send"
            " (empty content, closed reply window, or platform error;"
            " see gateway logs). Do not retry with identical content."
        )
    status = result.get("status")
    if status == "pending_audit":
        return "Accepted: QQ queued the message for manual review before delivery."
    if status == "already_sent":
        return "Skipped: identical content was already sent for this reply window."
    return "Sent."


@tool(name="qq.version", agent_use=False)
def qq_version() -> str:
    """Show the installed bub-qq plugin version (ops comma command)."""

    try:
        return f"bub-qq {package_version('bub-qq')}"
    except PackageNotFoundError:
        return "bub-qq (version unknown: package metadata not found)"
