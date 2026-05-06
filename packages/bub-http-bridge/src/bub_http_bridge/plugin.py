from bub import hookimpl
from bub.channels import Channel
from bub.types import MessageHandler


class HttpBridgeImpl:
    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        from bub_http_bridge.channel import HttpBridgeChannel

        return [HttpBridgeChannel(on_receive=message_handler)]


main = HttpBridgeImpl()
