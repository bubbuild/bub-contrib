from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from bub import hookimpl
from bub_extism.bridge import ExtismBridge
from bub_extism.channel import channels_from_descriptors
from bub_extism.cli import register_cli_commands
from bub_extism.codec import state_to_json, to_json_value
from bub_extism.config import ExtismSettings
from bub_extism.stream import stream_events_from_value
from bub_extism.tape_store import tape_store_from_descriptor
from republic import AsyncStreamEvents, TapeContext
from republic.tape.context import LAST_ANCHOR

if TYPE_CHECKING:
    import typer
    from bub.channels import Channel
    from bub.framework import BubFramework
    from bub.types import Envelope, MessageHandler, State
    from republic.tape import TapeStore


class ExtismPlugin:
    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self.settings = ExtismSettings()
        self.bridge = ExtismBridge(self.settings)
        self._register_model_hook_adapter()

    def _register_model_hook_adapter(self) -> None:
        config = self.bridge.selected_config()
        if config is None:
            return

        plugin_manager = getattr(self.framework, "_plugin_manager", None)
        if plugin_manager is None:
            return

        if config.hooks.run_model_stream is not None:
            plugin_manager.register(
                _ExtismRunModelStreamPlugin(self.bridge),
                name="extism-run-model-stream",
            )
            return

        if config.hooks.run_model is not None:
            plugin_manager.register(
                _ExtismRunModelPlugin(self.bridge),
                name="extism-run-model",
            )

    @hookimpl
    def resolve_session(self, message: Envelope) -> str | None:
        value = self.bridge.call_hook_sync(
            "resolve_session",
            {"message": to_json_value(message)},
        )
        if value is None:
            return None
        return str(value)

    @hookimpl
    async def build_prompt(
        self,
        message: Envelope,
        session_id: str,
        state: State,
    ) -> str | list[dict[str, Any]] | None:
        value = await self.bridge.call_hook(
            "build_prompt",
            {
                "message": to_json_value(message),
                "session_id": session_id,
                "state": state_to_json(state),
            },
        )
        if value is None:
            return None
        if isinstance(value, str | list):
            return cast(str | list[dict[str, Any]], value)
        raise RuntimeError("Extism build_prompt must return a string or content-part list")

    @hookimpl
    async def load_state(self, message: Envelope, session_id: str) -> State | None:
        value = await self.bridge.call_hook(
            "load_state",
            {"message": to_json_value(message), "session_id": session_id},
        )
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("Extism load_state must return an object")
        return value

    @hookimpl
    async def save_state(
        self,
        session_id: str,
        state: State,
        message: Envelope,
        model_output: str,
    ) -> None:
        await self.bridge.call_hook(
            "save_state",
            {
                "session_id": session_id,
                "state": state_to_json(state),
                "message": to_json_value(message),
                "model_output": model_output,
            },
        )

    @hookimpl
    def render_outbound(
        self,
        message: Envelope,
        session_id: str,
        state: State,
        model_output: str,
    ) -> list[Envelope]:
        value = self.bridge.call_hook_sync(
            "render_outbound",
            {
                "message": to_json_value(message),
                "session_id": session_id,
                "state": state_to_json(state),
                "model_output": model_output,
            },
        )
        if value is None:
            return []
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return value
        raise RuntimeError("Extism render_outbound must return an envelope or envelope list")

    @hookimpl
    async def dispatch_outbound(self, message: Envelope) -> bool:
        value = await self.bridge.call_hook(
            "dispatch_outbound",
            {"message": to_json_value(message)},
        )
        return bool(value)

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        config = self.bridge.selected_config()
        if config is None or config.hooks.register_cli_commands is None:
            return
        descriptors = self.bridge.call_hook_sync("register_cli_commands", {"commands": []}, config=config)
        register_cli_commands(app, self.bridge, config, descriptors)

    @hookimpl
    def onboard_config(self, current_config: dict[str, Any]) -> dict[str, Any] | None:
        value = self.bridge.call_hook_sync(
            "onboard_config",
            {"current_config": to_json_value(current_config)},
        )
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("Extism onboard_config must return an object")
        return value

    @hookimpl
    async def on_error(self, stage: str, error: Exception, message: Envelope | None) -> None:
        await self.bridge.call_hook(
            "on_error",
            {
                "stage": stage,
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
                "message": to_json_value(message),
            },
        )

    @hookimpl
    def system_prompt(self, prompt: str | list[dict[str, Any]], state: State) -> str | None:
        value = self.bridge.call_hook_sync(
            "system_prompt",
            {"prompt": prompt, "state": state_to_json(state)},
        )
        if value is None:
            return None
        if not isinstance(value, str):
            raise RuntimeError("Extism system_prompt must return a string")
        return value

    @hookimpl
    def provide_tape_store(self) -> TapeStore | None:
        config = self.bridge.selected_config()
        if config is None or config.hooks.provide_tape_store is None:
            return None
        descriptor = self.bridge.call_hook_sync("provide_tape_store", {}, config=config)
        return tape_store_from_descriptor(self.bridge, config, descriptor)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        config = self.bridge.selected_config()
        if config is None or config.hooks.provide_channels is None:
            return []
        descriptors = self.bridge.call_hook_sync("provide_channels", {}, config=config)
        return channels_from_descriptors(self.bridge, config, descriptors, message_handler)

    @hookimpl
    def build_tape_context(self) -> TapeContext | None:
        value = self.bridge.call_hook_sync("build_tape_context", {})
        if value is None:
            return None
        if not isinstance(value, dict):
            raise RuntimeError("Extism build_tape_context must return an object")

        anchor_value = value.get("anchor", "last")
        if anchor_value is None:
            anchor = None
        elif str(anchor_value).lower() in {"last", "last_anchor"}:
            anchor = LAST_ANCHOR
        else:
            anchor = str(anchor_value)

        state = value.get("state", {})
        if not isinstance(state, dict):
            raise RuntimeError("Extism build_tape_context state must be an object")
        return TapeContext(anchor=anchor, state=state)


class _ExtismRunModelPlugin:
    def __init__(self, bridge: ExtismBridge) -> None:
        self.bridge = bridge

    @hookimpl
    async def run_model(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> str | None:
        value = await self.bridge.call_hook(
            "run_model",
            {
                "prompt": prompt,
                "session_id": session_id,
                "state": state_to_json(state),
            },
        )
        if value is None:
            return None
        if not isinstance(value, str):
            raise RuntimeError("Extism run_model must return a string")
        return value


class _ExtismRunModelStreamPlugin:
    def __init__(self, bridge: ExtismBridge) -> None:
        self.bridge = bridge

    @hookimpl
    async def run_model_stream(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: State,
    ) -> AsyncStreamEvents | None:
        value = await self.bridge.call_hook(
            "run_model_stream",
            {
                "prompt": prompt,
                "session_id": session_id,
                "state": state_to_json(state),
            },
        )
        return stream_events_from_value(value)
