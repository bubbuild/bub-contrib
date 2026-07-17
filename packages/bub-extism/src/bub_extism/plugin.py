from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bub import hookimpl
from bub.streaming import AsyncStreamEvents
from bub.tape import LAST_ANCHOR, TapeContext
from bub_extism.bridge import ExtismBridge
from bub_extism.channel import channels_from_value
from bub_extism.cli import register_cli_commands
from bub_extism.codec import (
    error_to_json,
    mapping_to_json,
    message_to_json,
    state_to_json,
)
from bub_extism.config import ExtismPluginConfig, ExtismSettings, PLUGIN_HOOK_NAMES
from bub_extism.stream import stream_events_from_value
from bub_extism.tape_store import tape_store_from_value

if TYPE_CHECKING:
    import typer
    from bub.channels import Channel
    from bub.framework import BubFramework
    from bub.channels.contracts import MessageHandler
    from bub.envelope import Envelope
    from bub.turn import TurnState
    from bub.tape import TapeStore


def _message_args(message: Envelope) -> dict[str, Any]:
    return {"message": message_to_json(message)}


def _message_session_args(message: Envelope, session_id: str) -> dict[str, Any]:
    return {
        **_message_args(message),
        "session_id": session_id,
    }


def _state_args(state: TurnState) -> dict[str, Any]:
    return {"state": state_to_json(state)}


def _message_session_state_args(
    message: Envelope, session_id: str, state: TurnState
) -> dict[str, Any]:
    return {
        **_message_session_args(message, session_id),
        **_state_args(state),
    }


def _prompt_session_state_args(
    prompt: str | list[dict[str, Any]],
    session_id: str,
    state: TurnState,
) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "session_id": session_id,
        **_state_args(state),
    }


def _require_mapping(value: Any, *, hook_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Extism {hook_name} must return an object")
    return value


def _require_string(value: Any, *, hook_name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"Extism {hook_name} must return a string")
    return value


def _optional_mapping(value: Any, *, hook_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _require_mapping(value, hook_name=hook_name)


def _optional_string(value: Any, *, hook_name: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, hook_name=hook_name)


def _prompt_value(value: Any) -> str | list[dict[str, Any]] | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list):
        return value
    raise RuntimeError("Extism build_prompt must return a string or content-part list")


def _outbound_messages(value: Any) -> list[Envelope]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise RuntimeError(
        "Extism render_outbound must return an envelope or envelope list"
    )


def _tape_context(value: Any) -> TapeContext | None:
    if value is None:
        return None

    data = _require_mapping(value, hook_name="build_tape_context")
    anchor_value = data.get("anchor", "last")
    if anchor_value is None:
        anchor = None
    elif str(anchor_value).lower() in {"last", "last_anchor"}:
        anchor = LAST_ANCHOR
    else:
        anchor = str(anchor_value)

    state = _require_mapping(
        data.get("state", {}), hook_name="build_tape_context state"
    )
    return TapeContext(anchor=anchor, state=state)


class ExtismPlugin:
    def __init__(
        self,
        framework: BubFramework,
        *,
        settings: ExtismSettings | None = None,
    ) -> None:
        self.framework = framework
        self.settings = settings or ExtismSettings()
        self.bridge = ExtismBridge()
        self._register_hook_adapters()

    def _register_hook_adapters(self) -> None:
        plugin_manager = getattr(self.framework, "_plugin_manager", None)
        if plugin_manager is None:
            return

        for plugin_name, config in self.settings.read_config().plugins.items():
            adapter = build_hook_adapter(plugin_name, self.bridge, config)
            if adapter is not None:
                plugin_manager.register(adapter, name=f"extism:{plugin_name}")

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        register_cli_commands(app, self.settings, self.bridge)


class ExtismHookAdapter:
    def __init__(self, bridge: ExtismBridge, config: ExtismPluginConfig) -> None:
        self.bridge = bridge
        self.config = config

    def _call_sync(self, hook_name: str, **args: Any) -> Any:
        return self.bridge.call_hook_sync(hook_name, args, config=self.config)

    async def _call(self, hook_name: str, **args: Any) -> Any:
        return await self.bridge.call_hook(hook_name, args, config=self.config)

    def hook_resolve_session(self, message: Envelope) -> str | None:
        value = self._call_sync("resolve_session", **_message_args(message))
        return None if value is None else str(value)

    async def hook_build_prompt(
        self,
        message: Envelope,
        session_id: str,
        state: TurnState,
    ) -> str | list[dict[str, Any]] | None:
        return _prompt_value(
            await self._call(
                "build_prompt",
                **_message_session_state_args(message, session_id, state),
            )
        )

    async def hook_load_state(
        self, message: Envelope, session_id: str
    ) -> TurnState | None:
        return _optional_mapping(
            await self._call(
                "load_state", **_message_session_args(message, session_id)
            ),
            hook_name="load_state",
        )

    async def hook_save_state(
        self,
        session_id: str,
        state: TurnState,
        message: Envelope,
        model_output: str,
    ) -> None:
        await self._call(
            "save_state",
            **_message_session_state_args(message, session_id, state),
            model_output=model_output,
        )

    def hook_render_outbound(
        self,
        message: Envelope,
        session_id: str,
        state: TurnState,
        model_output: str,
    ) -> list[Envelope]:
        return _outbound_messages(
            self._call_sync(
                "render_outbound",
                **_message_session_state_args(message, session_id, state),
                model_output=model_output,
            )
        )

    async def hook_dispatch_outbound(self, message: Envelope) -> bool:
        return bool(await self._call("dispatch_outbound", **_message_args(message)))

    def hook_onboard_config(
        self, current_config: dict[str, Any]
    ) -> dict[str, Any] | None:
        return _optional_mapping(
            self._call_sync(
                "onboard_config", current_config=mapping_to_json(current_config)
            ),
            hook_name="onboard_config",
        )

    async def hook_on_error(
        self,
        stage: str,
        error: Exception,
        message: Envelope | None,
    ) -> None:
        await self._call(
            "on_error",
            stage=stage,
            error=error_to_json(error),
            message=None if message is None else message_to_json(message),
        )

    def hook_system_prompt(
        self, prompt: str | list[dict[str, Any]], state: TurnState
    ) -> str | None:
        return _optional_string(
            self._call_sync("system_prompt", prompt=prompt, **_state_args(state)),
            hook_name="system_prompt",
        )

    def hook_provide_tape_store(self) -> TapeStore | None:
        return tape_store_from_value(
            self.bridge,
            self.config,
            self._call_sync("provide_tape_store"),
        )

    def hook_provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        return channels_from_value(
            self.bridge,
            self.config,
            self._call_sync("provide_channels"),
            message_handler,
        )

    def hook_build_tape_context(self) -> TapeContext | None:
        return _tape_context(self._call_sync("build_tape_context"))

    async def hook_run_model(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: TurnState,
    ) -> str | None:
        return _optional_string(
            await self._call(
                "run_model",
                **_prompt_session_state_args(prompt, session_id, state),
            ),
            hook_name="run_model",
        )

    async def hook_run_model_stream(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: TurnState,
    ) -> AsyncStreamEvents | None:
        value = await self._call(
            "run_model_stream",
            **_prompt_session_state_args(prompt, session_id, state),
        )
        return stream_events_from_value(value)


def build_hook_adapter(
    plugin_name: str,
    bridge: ExtismBridge,
    config: ExtismPluginConfig,
) -> ExtismHookAdapter | None:
    enabled_hook_names = tuple(
        hook_name for hook_name in PLUGIN_HOOK_NAMES if hook_name in config.hooks
    )
    if not enabled_hook_names:
        return None

    attrs = {
        hook_name: hookimpl(getattr(ExtismHookAdapter, f"hook_{hook_name}"))
        for hook_name in enabled_hook_names
    }
    adapter_type = type(
        f"ExtismHookAdapter_{_class_name_fragment(plugin_name)}",
        (ExtismHookAdapter,),
        attrs,
    )
    return adapter_type(bridge, config)


def _class_name_fragment(plugin_name: str) -> str:
    text = "".join(char if char.isalnum() else "_" for char in plugin_name.strip())
    return text or "Plugin"
