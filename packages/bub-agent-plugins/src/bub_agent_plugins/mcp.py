"""Validate portable MCP configuration and adapt it to bub-mcp."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from bub_agent_plugins.documents import read_json_object
from bub_agent_plugins.models import AgentPluginManifest

HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
PLUGIN_VARIABLE_PATTERN = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")


class MCPConfigError(ValueError):
    """An error that disables MCP for one plugin."""


def load_mcp_servers(
    manifest: AgentPluginManifest, data_root: Path
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    config_path = manifest.root / "mcp.json"
    if not config_path.exists():
        return {}, []
    if not config_path.is_file() or not is_contained(config_path, manifest.root):
        raise MCPConfigError("MCP component is not a contained regular file")

    try:
        document = read_json_object(config_path)
    except (OSError, ValueError) as exc:
        raise MCPConfigError(str(exc)) from exc

    error = first_error(manifest.schema.mcp_document(), document)
    if error is not None:
        raise MCPConfigError(error)

    plugin_data = prepare_plugin_data(data_root, manifest.name)
    servers: dict[str, dict[str, Any]] = {}
    diagnostics: list[str] = []
    validator = Draft202012Validator(manifest.schema.mcp_server())
    for server_name, config in document["mcpServers"].items():
        errors = sorted(validator.iter_errors(config), key=lambda item: list(item.path))
        if errors:
            diagnostics.append(
                f"{config_path}#{server_name}: invalid MCP server skipped: "
                f"{errors[0].message}"
            )
            continue
        try:
            adapted = adapt_mcp_server(
                config,
                plugin_root=manifest.root,
                plugin_data=plugin_data,
            )
        except ValueError as exc:
            diagnostics.append(
                f"{config_path}#{server_name}: invalid MCP server skipped: {exc}"
            )
            continue
        servers[f"{manifest.name}.{server_name}"] = adapted
    return servers, diagnostics


def first_error(schema: dict[str, Any], document: object) -> str | None:
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda item: list(item.path),
    )
    return errors[0].message if errors else None


def prepare_plugin_data(data_root: Path, plugin_name: str) -> Path:
    plugin_data = data_root / plugin_name
    plugin_data.mkdir(parents=True, exist_ok=True)
    resolved = plugin_data.resolve()
    if not resolved.is_relative_to(data_root):
        raise MCPConfigError("PLUGIN_DATA escapes the configured data root")
    return resolved


def adapt_mcp_server(
    config: dict[str, Any], *, plugin_root: Path, plugin_data: Path
) -> dict[str, Any]:
    server_type = config["type"]
    if server_type == "stdio":
        command = resolve_command(config["command"], plugin_root)
        args = [
            expand_plugin_variables(value, plugin_root, plugin_data)
            for value in config.get("args", [])
        ]
        env = {
            key: expand_plugin_variables(value, plugin_root, plugin_data)
            for key, value in config.get("env", {}).items()
        }
        if os.name == "nt" and {key.casefold() for key in env} & {
            "plugin_root",
            "plugin_data",
        }:
            raise ValueError(
                "reserved environment names are case-insensitive on Windows"
            )
        env["PLUGIN_ROOT"] = str(plugin_root)
        env["PLUGIN_DATA"] = str(plugin_data)
        return {
            "command": command,
            "args": args,
            "env": env,
            "cwd": str(resolve_cwd(config.get("cwd"), plugin_root, plugin_data)),
            "transport": "stdio",
        }

    url = config["url"]
    validate_remote_url(url)
    headers = config.get("headers", {})
    validate_headers(headers)
    return {"url": url, "headers": dict(headers), "transport": server_type}


def resolve_command(command: str, plugin_root: Path) -> str:
    if command.startswith("./"):
        resolved = (plugin_root / command[2:]).resolve()
        if not resolved.is_relative_to(plugin_root):
            raise ValueError("command escapes the plugin root")
        return str(resolved)
    if (
        command in {".", ".."}
        or "/" in command
        or "\\" in command
        or Path(command).is_absolute()
        or PureWindowsPath(command).is_absolute()
    ):
        raise ValueError("command must be a bare executable or start with ./")
    return command


def resolve_cwd(cwd: str | None, plugin_root: Path, plugin_data: Path) -> Path:
    if cwd is None:
        return plugin_root
    expanded = expand_plugin_variables(cwd, plugin_root, plugin_data)
    resolved = (
        (plugin_root / expanded).resolve()
        if cwd.startswith("./")
        else Path(expanded).resolve()
    )
    expected_root = plugin_data if cwd.startswith("${PLUGIN_DATA}") else plugin_root
    if not resolved.is_relative_to(expected_root):
        raise ValueError("cwd escapes its declared plugin root")
    return resolved


def expand_plugin_variables(value: str, plugin_root: Path, plugin_data: Path) -> str:
    replacements = {
        "PLUGIN_ROOT": str(plugin_root),
        "PLUGIN_DATA": str(plugin_data),
    }
    return PLUGIN_VARIABLE_PATTERN.sub(
        lambda match: replacements[match.group(1)], value
    )


def validate_remote_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid MCP URL: {exc}") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
    ):
        raise ValueError("URL must be absolute HTTP(S) without user info or fragment")
    if parsed.scheme == "http" and not is_loopback_host(host):
        raise ValueError("non-loopback MCP URLs must use HTTPS")


def is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_headers(headers: dict[str, str]) -> None:
    normalized_names: set[str] = set()
    for name, value in headers.items():
        normalized = name.casefold()
        if not HEADER_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid HTTP header name: {name}")
        if normalized in normalized_names:
            raise ValueError(f"duplicate case-insensitive HTTP header: {name}")
        if any(
            ord(character) > 0x7E or (ord(character) < 0x20 and character != "\t")
            for character in value
        ):
            raise ValueError(f"invalid HTTP header value for {name}")
        normalized_names.add(normalized)


def is_contained(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def create_agent_plugin_client(
    server_name: str,
    config: dict[str, Any],
    init_timeout_seconds: float | None,
) -> Any:
    """Create a FastMCP client that does not leak headers across origins."""
    import fastmcp

    if "url" not in config:
        options = (
            {"init_timeout": init_timeout_seconds}
            if init_timeout_seconds is not None
            else {}
        )
        return fastmcp.Client({server_name: config}, **options)

    from fastmcp.client.transports import SSETransport, StreamableHttpTransport

    headers = config.get("headers", {})
    client_factory = same_origin_http_client(config["url"]) if headers else None
    transport_type = (
        SSETransport if config["transport"] == "sse" else StreamableHttpTransport
    )
    transport = transport_type(
        config["url"],
        headers=headers,
        httpx_client_factory=client_factory,
    )
    return fastmcp.Client(transport, init_timeout=init_timeout_seconds)


def same_origin_http_client(origin_url: str) -> Callable[..., Any]:
    """Build MCP HTTP clients that keep configured headers on one origin."""
    import httpx
    from mcp.shared._httpx_utils import create_mcp_http_client

    origin = url_origin(origin_url)

    async def reject_cross_origin_request(request: httpx.Request) -> None:
        if url_origin(str(request.url)) != origin:
            raise httpx.RequestError(
                f"cross-origin MCP request rejected: {request.url}", request=request
            )

    def create_client(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        follow_redirects: bool = True,
    ) -> httpx.AsyncClient:
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        client.follow_redirects = follow_redirects
        client.event_hooks["request"].append(reject_cross_origin_request)
        return client

    return create_client


def url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = {"http": 80, "https": 443}.get(parsed.scheme.casefold())
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), port
