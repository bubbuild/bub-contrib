from __future__ import annotations

import asyncio
import json
from typing import Any

from bub_extism.codec import ExtismHookSkip, build_request, decode_response
from bub_extism.config import ExtismPluginConfig, ExtismSettings


class ExtismBridge:
    def __init__(self, settings: ExtismSettings) -> None:
        self.settings = settings

    def selected_config(self) -> ExtismPluginConfig | None:
        return self.settings.read_config().selected_plugin()

    def function_name(self, hook_name: str) -> str | None:
        config = self.selected_config()
        if config is None:
            return None
        return getattr(config.hooks, hook_name)

    def call_hook_sync(
        self,
        hook_name: str,
        args: dict[str, Any],
        *,
        config: ExtismPluginConfig | None = None,
        function_name: str | None = None,
    ) -> Any:
        selected = config or self.selected_config()
        if selected is None:
            return None

        export_name = function_name or getattr(selected.hooks, hook_name)
        if export_name is None:
            return None

        try:
            return self._call_export(selected, export_name, hook_name, args)
        except ExtismHookSkip:
            return None

    async def call_hook(
        self,
        hook_name: str,
        args: dict[str, Any],
        *,
        config: ExtismPluginConfig | None = None,
        function_name: str | None = None,
    ) -> Any:
        return await asyncio.to_thread(
            self.call_hook_sync,
            hook_name,
            args,
            config=config,
            function_name=function_name,
        )

    def _call_export(
        self,
        config: ExtismPluginConfig,
        function_name: str,
        hook_name: str,
        args: dict[str, Any],
    ) -> Any:
        extism = _import_extism()
        request = build_request(hook_name, args)
        with extism.Plugin(
            config.plugin_input(),
            wasi=config.wasi,
            config=config.config or None,
        ) as plugin:
            if hasattr(plugin, "function_exists") and not plugin.function_exists(function_name):
                raise ExtismHookSkip

            raw_result = plugin.call(
                function_name,
                json.dumps(request, ensure_ascii=False),
            )
        return decode_response(raw_result, hook_name=hook_name)


def _import_extism() -> Any:
    try:
        import extism
    except ImportError as exc:
        raise RuntimeError("bub-extism requires the 'extism' runtime package") from exc
    return extism
