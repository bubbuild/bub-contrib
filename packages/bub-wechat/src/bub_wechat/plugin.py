import typer
from bub import BubFramework, hookimpl
from bub.builtin.auth import app as auth_app
from bub.channels import Channel
from bub.types import MessageHandler

from bub_wechat.channel import TOKEN_PATH, WeChatChannel


@auth_app.command()
def wechat():
    """Login to WeChat agent account."""
    from weixin_bot import WeixinBot

    bot = WeixinBot(token_path=str(TOKEN_PATH))
    bot.login()
    typer.echo("Login successful! You can now start the Bub agent with WeChat channel.")


class WechatPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        return [WeChatChannel(on_receive=message_handler)]
