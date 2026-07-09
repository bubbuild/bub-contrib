from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from typing import Any

from bub.channels import Channel
from bub.types import Envelope, MessageHandler
from republic import StreamEvent

from bub_extism.bridge import ExtismBridge
from bub_extism.codec import message_to_json
from bub_extism.config import ExtismPluginConfig
from bub_extism.descriptors import (
    normalize_function_bindings,
    require_mapping,
    required_text,
)


class ExtismChannel(Channel):
    def __init__(
        self,
        bridge: ExtismBridge,
        config: ExtismPluginConfig,
        *,
        name: str,
        enabled: bool,
        needs_debounce: bool,
        poll_interval_seconds: float,
        functions: dict[str, str],
        message_handler: MessageHandler,
    ) -> None:
        self.bridge = bridge
        self.config = config
        self.name = name
        self._enabled = enabled
        self._needs_debounce = needs_debounce
        self._message_handler = message_handler
        self._functions = functions
        self._poll_interval_seconds = poll_interval_seconds

    @classmethod
    def from_descriptor(
        cls,
        bridge: ExtismBridge,
        config: ExtismPluginConfig,
        descriptor: Any,
        message_handler: MessageHandler,
    ) -> ExtismChannel:
        data = require_mapping(
            descriptor, message="Extism channel descriptor must be an object"
        )
        name = required_text(
            data.get("name"), message="Extism channel descriptor must include a name"
        )
        return cls(
            bridge,
            config,
            name=name,
            enabled=bool(data.get("enabled", True)),
            needs_debounce=bool(data.get("needsDebounce", False)),
            poll_interval_seconds=float(data.get("pollIntervalSeconds", 1.0)),
            functions=_functions_from_descriptor(data),
            message_handler=message_handler,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def needs_debounce(self) -> bool:
        return self._needs_debounce

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
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self._poll_interval_seconds
                )
            except TimeoutError:
                continue

    async def stop(self) -> None:
        await self._call("stop", {})

    async def send(self, message: Envelope) -> None:
        await self._call("send", {"message": message_to_json(message)})

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


def channels_from_value(
    bridge: ExtismBridge,
    config: ExtismPluginConfig,
    value: Any,
    message_handler: MessageHandler,
) -> list[ExtismChannel]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("channels", [])
    if not isinstance(value, list):
        raise RuntimeError(
            "Extism provide_channels must return a list of channel descriptors"
        )
    return [
        ExtismChannel.from_descriptor(bridge, config, descriptor, message_handler)
        for descriptor in value
    ]


def _messages_from_value(value: Any) -> list[Envelope]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("messages", [value])
    if not isinstance(value, list):
        raise RuntimeError("Extism channel poll must return a message or message list")
    return value


def _functions_from_descriptor(descriptor: dict[str, Any]) -> dict[str, str]:
    return normalize_function_bindings(
        descriptor.get("functions"),
        message="Extism channel functions must be an object",
        missing_ok=True,
    )
