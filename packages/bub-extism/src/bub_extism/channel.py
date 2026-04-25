from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from typing import Any

from bub.channels import Channel
from bub.types import Envelope, MessageHandler
from republic import StreamEvent

from bub_extism.bridge import ExtismBridge
from bub_extism.codec import to_json_value
from bub_extism.config import ExtismPluginConfig


class ExtismChannel(Channel):
    def __init__(
        self,
        bridge: ExtismBridge,
        config: ExtismPluginConfig,
        descriptor: dict[str, Any],
        message_handler: MessageHandler,
    ) -> None:
        self.bridge = bridge
        self.config = config
        self.descriptor = descriptor
        self.name = str(descriptor["name"])
        self._message_handler = message_handler
        self._functions = dict(descriptor.get("functions") or {})
        self._poll_interval_seconds = float(descriptor.get("pollIntervalSeconds", 1.0))

    @property
    def enabled(self) -> bool:
        return bool(self.descriptor.get("enabled", True))

    @property
    def needs_debounce(self) -> bool:
        return bool(self.descriptor.get("needsDebounce", False))

    async def start(self, stop_event: asyncio.Event) -> None:
        await self._call("start", {})
        if "poll" not in self._functions:
            await stop_event.wait()
            return

        while not stop_event.is_set():
            messages = await self._call("poll", {})
            for message in _messages_from_value(messages):
                await self._message_handler(message)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        await self._call("stop", {})

    async def send(self, message: Envelope) -> None:
        await self._call("send", {"message": to_json_value(message)})

    def stream_events(
        self,
        message: Envelope,
        stream: AsyncIterable[StreamEvent],
    ) -> AsyncIterable[StreamEvent]:
        return stream

    async def _call(self, operation: str, args: dict[str, Any]) -> Any:
        function_name = self._functions.get(operation)
        if not isinstance(function_name, str) or not function_name:
            return None
        return await self.bridge.call_hook(
            f"channel.{operation}",
            {"channel": self.name, **args},
            config=self.config,
            function_name=function_name,
        )


def channels_from_descriptors(
    bridge: ExtismBridge,
    config: ExtismPluginConfig,
    descriptors: Any,
    message_handler: MessageHandler,
) -> list[ExtismChannel]:
    if descriptors is None:
        return []
    if isinstance(descriptors, dict):
        descriptors = descriptors.get("channels", [])
    if not isinstance(descriptors, list):
        raise RuntimeError("Extism provide_channels must return a list of channel descriptors")

    channels: list[ExtismChannel] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not descriptor.get("name"):
            raise RuntimeError("Extism channel descriptor must include a name")
        channels.append(ExtismChannel(bridge, config, descriptor, message_handler))
    return channels


def _messages_from_value(value: Any) -> list[Envelope]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("messages", [value])
    if not isinstance(value, list):
        raise RuntimeError("Extism channel poll must return a message or message list")
    return value
