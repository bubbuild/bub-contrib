from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from bub_extism.bridge import ExtismBridge
from bub_extism.config import (
    CLI_HOOK_NAME,
    ExtismConfig,
    ExtismPluginConfig,
    ExtismSettings,
    normalize_hook_bindings,
)
from bub_extism.descriptors import require_mapping, required_text

if TYPE_CHECKING:
    from collections.abc import Iterable


RESERVED_COMMAND_NAMES = {"add", "list", "remove", "show"}


@dataclass(frozen=True)
class CommandDescriptor:
    name: str
    function: str
    help_text: str | None = None

    @classmethod
    def from_descriptor(cls, descriptor: Any) -> CommandDescriptor:
        data = require_mapping(descriptor, message="Extism CLI command descriptor must be an object")
        name = required_text(data.get("name"), message="Extism CLI command descriptor requires name and function")
        function = required_text(
            data.get("function"),
            message="Extism CLI command descriptor requires name and function",
        )
        help_value = data.get("help")
        help_text = None if help_value is None else str(help_value)
        return cls(name=name, function=function, help_text=help_text)


def register_cli_commands(
    app: typer.Typer,
    settings: ExtismSettings,
    bridge: ExtismBridge,
) -> None:
    app.add_typer(make_extism_command(settings, bridge), name="extism")


def make_extism_command(settings: ExtismSettings, bridge: ExtismBridge) -> typer.Typer:
    app = typer.Typer(help="Manage and inspect Extism-backed Bub adapters.")

    @app.command("list")
    def list_plugins() -> None:
        """List configured Extism adapters."""
        config = settings.read_config()
        if not config.plugins:
            typer.echo("No Extism plugins configured.")
            typer.echo(f"Config: {settings.config_path}")
            return
        typer.echo(_format_plugin_list(config))
        typer.echo(f"Config: {settings.config_path}")

    @app.command("show")
    def show_plugin(name: str = typer.Argument(..., help="Configured adapter name.")) -> None:
        """Show one adapter configuration."""
        plugin = settings.read_config().plugins.get(name)
        if plugin is None:
            typer.echo(f"Extism plugin '{name}' does not exist.", err=True)
            raise typer.Exit(code=1)
        typer.echo(json.dumps(plugin.model_dump(mode="json"), ensure_ascii=False, indent=2))

    @app.command("add")
    def add_plugin(
        name: str = typer.Argument(..., help="Adapter name."),
        manifest_path: Path = typer.Argument(
            ...,
            exists=True,
            dir_okay=False,
            help="Path to an Extism manifest JSON file.",
        ),
        hook: list[str] | None = typer.Option(
            None,
            "--hook",
            help="Hook binding in HOOK=EXPORT format. Repeat to bind multiple Bub hooks.",
        ),
        wasi: bool = typer.Option(False, "--wasi", help="Enable WASI for this adapter."),
        replace: bool = typer.Option(False, "--replace", help="Replace an existing adapter with the same name."),
    ) -> None:
        """Add one Extism adapter."""
        config = settings.read_config()
        if name in config.plugins and not replace:
            raise typer.BadParameter(f"Extism plugin '{name}' already exists. Use --replace to overwrite it.")

        config.plugins[name] = ExtismPluginConfig(
            manifest=_load_manifest(manifest_path),
            hooks=_parse_hook_bindings(hook or []),
            wasi=wasi,
        )
        settings.write_config(config)

        typer.echo(f"Added Extism plugin '{name}'.")
        typer.echo(f"Config: {settings.config_path}")
        typer.echo(_format_single_plugin(name, config.plugins[name]))

    @app.command("remove")
    def remove_plugin(name: str = typer.Argument(..., help="Adapter name.")) -> None:
        """Remove one Extism adapter."""
        config = settings.read_config()
        if name not in config.plugins:
            typer.echo(f"Extism plugin '{name}' does not exist.", err=True)
            raise typer.Exit(code=1)

        del config.plugins[name]
        settings.write_config(config)
        typer.echo(f"Removed Extism plugin '{name}'.")
        typer.echo(f"Config: {settings.config_path}")

    _register_plugin_commands(app, settings.read_config(), bridge)
    return app


def _register_plugin_commands(app: typer.Typer, config: ExtismConfig, bridge: ExtismBridge) -> None:
    registered_names = set(RESERVED_COMMAND_NAMES)
    for plugin_name, plugin_config in config.plugins.items():
        if CLI_HOOK_NAME not in plugin_config.hooks:
            continue
        for descriptor in commands_from_value(
            bridge.call_hook_sync(
                "register_cli_commands",
                {"commands": []},
                config=plugin_config,
            )
        ):
            if descriptor.name in registered_names:
                raise RuntimeError(
                    f"Extism CLI command '{descriptor.name}' conflicts with an existing command"
                )
            registered_names.add(descriptor.name)

            help_text = descriptor.help_text or f"Run the '{descriptor.name}' command from Extism plugin '{plugin_name}'."
            app.command(descriptor.name, help=help_text)(
                _make_plugin_command(
                    bridge,
                    plugin_name,
                    plugin_config,
                    descriptor.name,
                    descriptor.function,
                )
            )


def commands_from_value(value: Any) -> list[CommandDescriptor]:
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("commands", [])
    if not isinstance(value, list):
        raise RuntimeError("Extism register_cli_commands must return a list")
    return [CommandDescriptor.from_descriptor(item) for item in value]


def _make_plugin_command(
    bridge: ExtismBridge,
    plugin_name: str,
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
            {
                "plugin": plugin_name,
                "command": command_name,
                "payload": args,
            },
            config=config,
            function_name=function_name,
        )
        if result is not None:
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))

    return command


def _format_plugin_list(config: ExtismConfig) -> str:
    lines = [typer.style("Extism Plugins", bold=True)]
    for name, plugin in config.plugins.items():
        lines.append(_format_single_plugin(name, plugin))
    return "\n".join(lines)


def _format_single_plugin(name: str, plugin: ExtismPluginConfig) -> str:
    hooks = plugin.hooks
    hook_text = ", ".join(f"{hook}->{export}" for hook, export in hooks.items()) if hooks else "No hooks"
    wasi_text = "enabled" if plugin.wasi else "disabled"
    return f"- {name}\n  WASI: {wasi_text}\n  Source: {_manifest_source(plugin.manifest)}\n  Hooks: {hook_text}"


def _manifest_source(manifest: dict[str, Any]) -> str:
    wasm_entries = manifest.get("wasm")
    if not isinstance(wasm_entries, list) or not wasm_entries:
        return "manifest"

    first_entry = wasm_entries[0]
    if not isinstance(first_entry, dict):
        return "manifest"
    for key in ("path", "url", "name"):
        value = first_entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return "manifest"


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("manifest file must contain valid JSON") from exc
    if not isinstance(raw, dict):
        raise typer.BadParameter("manifest file must contain a top-level object")
    return raw


def _parse_hook_bindings(bindings: Iterable[str]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for item in bindings:
        if "=" not in item:
            raise typer.BadParameter("--hook must be in HOOK=EXPORT format")
        hook_name, export_name = item.split("=", 1)
        hook_name = hook_name.strip()
        export_name = export_name.strip()
        if not hook_name or not export_name:
            raise typer.BadParameter("--hook requires both hook and export names")
        payload[hook_name] = export_name
    try:
        return normalize_hook_bindings(payload)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
