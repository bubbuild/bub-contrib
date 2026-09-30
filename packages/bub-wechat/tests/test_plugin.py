import asyncio

from bub.tools import ToolContext

from bub_wechat import plugin
from bub_wechat.channel import OutgoingMessage


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, OutgoingMessage]] = []

    async def send_outgoing(self, chat_id: str, message: OutgoingMessage) -> None:
        self.sent.append((chat_id, message))


def test_wechat_tool_returns_structured_result(monkeypatch) -> None:
    channel = FakeChannel()
    monkeypatch.setattr(plugin, "_channel", channel)
    context = ToolContext(tape=None, state={"session_id": "wechat:user-1"})

    result = asyncio.run(
        plugin.wechat_send.run(message={"text": "hello"}, context=context)
    )

    assert result == {"chat_id": "user-1"}
    assert plugin.wechat_send.render(result) == "Message sent to wechat."
    assert plugin.wechat_send.output_schema is not None
    assert channel.sent == [("user-1", OutgoingMessage(text="hello"))]
