import typer
from bub import BubFramework, hookimpl, tool
from bub.builtin.auth import app as auth_app
from bub.channels import Channel
from bub.tools import ToolContext
from bub.types import MessageHandler

from bub_wechat.channel import OutgoingMessage, WeChatChannel, get_token_path


@auth_app.command()
def wechat():
    """Login to WeChat agent account."""
    from weixin_bot import WeixinBot

    bot = WeixinBot(token_path=str(get_token_path()))
    bot.login()
    typer.echo("Login successful! You can now start the Bub agent with WeChat channel.")


_channel: WeChatChannel | None = None


@tool(name="wechat", context=True)
async def wechat_send(
    message: OutgoingMessage, chat_id: str | None = None, *, context: ToolContext
) -> str:
    if _channel is None:
        raise RuntimeError("wechat channel is not initialized")
    if chat_id is None:
        chat_id = context.state["session_id"].split(":")[-1]
    await _channel.send_outgoing(chat_id, message)  # type: ignore
    return "Message sent to wechat."


class WechatPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        global _channel
        _channel = WeChatChannel(on_receive=message_handler)
        return [_channel]
