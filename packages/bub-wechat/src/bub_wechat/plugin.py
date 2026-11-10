from typing import TypedDict, final

import typer
from bub import BubFramework, hookimpl, tool
from bub.builtin.auth import app as auth_app
from bub.channels import Channel
from bub.core.tools import Tool, ToolContext
from bub.channels.contracts import MessageHandler
from bub.turn import TurnState

from bub_wechat.channel import OutgoingMessage, WeChatChannel, get_token_path


@auth_app.command()
def wechat():
    """Login to WeChat agent account."""
    from weixin_bot import WeixinBot

    bot = WeixinBot(token_path=str(get_token_path()))
    bot.login()
    typer.echo("Login successful! You can now start the Bub agent with WeChat channel.")


_channel: WeChatChannel | None = None

REPLY_PROMPT = """\
<wechat_response_instruct>
This conversation is on the WeChat channel ($wechat), which overrides the generic channel guidance above:
- Your plain final reply text IS delivered to the WeChat chat as-is. To reply, write the reply text and end the turn.
- Do not call the wechat tool for ordinary text; it would send the same content twice. Use it only to attach an image, video or file.
- Never end with a confirmation such as "sent to WeChat"; the final reply is itself a chat message the user sees.
</wechat_response_instruct>"""


@final
class WechatSendResult(TypedDict):
    chat_id: str


@tool(
    name="wechat",
    context=True,
    renderer=lambda result: "Message sent to wechat.",
)
async def wechat_send(
    message: OutgoingMessage, chat_id: str | None = None, *, context: ToolContext
) -> WechatSendResult:
    if _channel is None:
        raise RuntimeError("wechat channel is not initialized")
    if chat_id is None:
        chat_id = context.state["session_id"].split(":")[-1]
    await _channel.send_outgoing(chat_id, message)  # type: ignore
    return {"chat_id": chat_id}


class WechatPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    @hookimpl
    def system_prompt(self, prompt: str | list[dict], state: TurnState) -> str | None:
        session_id = state.get("session_id") if isinstance(state, dict) else None
        if isinstance(session_id, str) and session_id.startswith("wechat:"):
            return REPLY_PROMPT
        return None

    @hookimpl
    def provide_tools(self) -> list[Tool]:
        return [wechat_send]

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        global _channel
        _channel = WeChatChannel(on_receive=message_handler)
        return [_channel]
