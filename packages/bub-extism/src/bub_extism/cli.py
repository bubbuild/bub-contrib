from __future__ import annotations

import json
from typing import Any

import typer

from bub_extism.bridge import ExtismBridge
from bub_extism.config import ExtismPluginConfig


def register_cli_commands(
    app: typer.Typer,
    bridge: ExtismBridge,
    config: ExtismPluginConfig,
    descriptors: Any,
) -> None:
    if descriptors is None:
        return
    if isinstance(descriptors, dict):
        descriptors = descriptors.get("commands", [])
    if not isinstance(descriptors, list):
        raise RuntimeError("Extism register_cli_commands must return a list")

    group = typer.Typer(help="Commands provided by Extism WebAssembly plugins.")
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise RuntimeError("Extism CLI command descriptor must be an object")
        name = str(descriptor.get("name", "")).strip()
        function_name = str(descriptor.get("function", "")).strip()
        if not name or not function_name:
            raise RuntimeError("Extism CLI command descriptor requires name and function")
        help_text = str(descriptor.get("help", "Run an Extism command."))
        group.command(name, help=help_text)(_make_command(bridge, config, name, function_name))

    app.add_typer(group, name="extism")


def _make_command(
    bridge: ExtismBridge,
    config: ExtismPluginConfig,
    command_name: str,
    function_name: str,
):
    def command(payload: str = typer.Argument("{}", help="JSON payload for the command.")) -> None:
        try:
            args = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter("payload must be valid JSON") from exc
        result = bridge.call_hook_sync(
            "cli_command",
            {"command": command_name, "payload": args},
            config=config,
            function_name=function_name,
        )
        if result is not None:
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))

    return command
