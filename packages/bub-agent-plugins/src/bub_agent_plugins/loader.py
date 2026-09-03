"""Validation and adaptation for Agent Plugins 1.0.0 packages."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from bub import skills as bub_skills
from jsonschema import Draft202012Validator

from bub_agent_plugins.schemas import (
    MCP_SCHEMA_ID,
    MCP_SERVER_SCHEMA,
    PLUGIN_SCHEMA,
    PLUGIN_SCHEMA_ID,
)

HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
PLUGIN_VARIABLE_PATTERN = re.compile(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}")
MANIFEST_FIELDS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}


@dataclass(frozen=True)
class AgentPluginManifest:
    name: str
    root: Path
    data: dict[str, Any]


@dataclass
class LoadedAgentPlugin:
    manifest: AgentPluginManifest
    skill_directories: list[Path] = field(default_factory=list)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class AgentPluginLoadResult:
    plugins: list[LoadedAgentPlugin] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def skill_directories(self) -> list[Path]:
        return [
            skill_dir
            for plugin in self.plugins
            for skill_dir in plugin.skill_directories
        ]

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return {
            name: config
            for plugin in self.plugins
            for name, config in plugin.mcp_servers.items()
        }


class AgentPluginLoader:
    """Load Agent Plugins while isolating failures at specification boundaries."""

    def __init__(
        self,
        *,
        data_root: Path,
        skills_enabled: bool = True,
        mcp_enabled: bool = True,
    ) -> None:
        self.data_root = data_root.expanduser().resolve()
        self.skills_enabled = skills_enabled
        self.mcp_enabled = mcp_enabled

    def load(self, roots: list[Path]) -> AgentPluginLoadResult:
        result = AgentPluginLoadResult()
        names: set[str] = set()
        resolved_roots: set[Path] = set()

        for candidate in roots:
            try:
                root = candidate.expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                result.diagnostics.append(
                    f"{candidate}: cannot resolve plugin root: {exc}"
                )
                continue
            if root in resolved_roots:
                continue
            resolved_roots.add(root)
            loaded, diagnostics = self._load_one(root)
            result.diagnostics.extend(diagnostics)
            if loaded is None:
                continue
            if loaded.manifest.name in names:
                result.diagnostics.append(
                    f"{root}: duplicate plugin name '{loaded.manifest.name}' ignored"
                )
                continue
            names.add(loaded.manifest.name)
            result.plugins.append(loaded)

        return result

    def _load_one(self, root: Path) -> tuple[LoadedAgentPlugin | None, list[str]]:
        diagnostics: list[str] = []
        manifest = self._load_manifest(root, diagnostics)
        if manifest is None:
            return None, diagnostics

        loaded = LoadedAgentPlugin(manifest=manifest, diagnostics=diagnostics)
        if self.skills_enabled:
            try:
                loaded.skill_directories = self._load_skills(manifest, diagnostics)
            except OSError as exc:
                diagnostics.append(
                    f"{manifest.root / 'skills'}: skills component disabled: {exc}"
                )
        if self.mcp_enabled:
            try:
                loaded.mcp_servers = self._load_mcp(manifest, diagnostics)
            except OSError as exc:
                diagnostics.append(
                    f"{manifest.root / 'mcp.json'}: MCP component disabled: {exc}"
                )
        return loaded, diagnostics

    def _load_manifest(
        self, root: Path, diagnostics: list[str]
    ) -> AgentPluginManifest | None:
        if not root.is_dir():
            diagnostics.append(f"{root}: plugin root is not a directory")
            return None

        manifest_path = root / "plugin.json"
        if not _is_contained(manifest_path, root) or not manifest_path.is_file():
            diagnostics.append(
                f"{root}: plugin.json is missing or escapes the plugin root"
            )
            return None

        try:
            data = _read_json_object(manifest_path)
        except (OSError, ValueError) as exc:
            diagnostics.append(f"{manifest_path}: invalid manifest: {exc}")
            return None

        unknown_fields = sorted(set(data) - MANIFEST_FIELDS)
        if unknown_fields:
            diagnostics.append(
                f"{manifest_path}: ignored unknown fields: {', '.join(unknown_fields)}"
            )

        validation_data = {
            key: value
            for key, value in data.items()
            if key in MANIFEST_FIELDS - {"extensions"}
        }
        if "extensions" in data and not isinstance(data["extensions"], dict):
            diagnostics.append(f"{manifest_path}: ignored non-object extensions field")

        errors = sorted(
            Draft202012Validator(PLUGIN_SCHEMA).iter_errors(validation_data),
            key=lambda error: list(error.path),
        )
        if errors:
            diagnostics.append(
                f"{manifest_path}: manifest rejected: {errors[0].message}"
            )
            return None

        return AgentPluginManifest(
            name=str(validation_data["name"]), root=root, data=data
        )

    def _load_skills(
        self, manifest: AgentPluginManifest, diagnostics: list[str]
    ) -> list[Path]:
        skills_root = manifest.root / "skills"
        if not skills_root.exists():
            return []
        if not skills_root.is_dir() or not _is_contained(skills_root, manifest.root):
            diagnostics.append(
                f"{skills_root}: skills component is not a contained directory"
            )
            return []

        skill_directories: list[Path] = []
        for skill_dir in sorted(skills_root.iterdir(), key=lambda path: path.name):
            skill_file = skill_dir / bub_skills.SKILL_FILE_NAME
            if not skill_dir.is_dir() or not skill_file.exists():
                continue
            if not skill_file.is_file() or not _tree_is_contained(
                skill_dir, manifest.root
            ):
                diagnostics.append(
                    f"{skill_file}: skill skipped because a package path escapes the plugin root"
                )
                continue
            if bub_skills._read_skill(skill_dir, source="builtin") is None:
                diagnostics.append(f"{skill_file}: invalid Agent Skill skipped")
                continue
            skill_directories.append(skill_dir.resolve())
        return skill_directories

    def _load_mcp(
        self, manifest: AgentPluginManifest, diagnostics: list[str]
    ) -> dict[str, dict[str, Any]]:
        config_path = manifest.root / "mcp.json"
        if not config_path.exists():
            return {}
        if not config_path.is_file() or not _is_contained(config_path, manifest.root):
            diagnostics.append(
                f"{config_path}: MCP component is not a contained regular file"
            )
            return {}

        try:
            data = _read_json_object(config_path)
        except (OSError, ValueError) as exc:
            diagnostics.append(f"{config_path}: MCP component disabled: {exc}")
            return {}

        if set(data) != {"$schema", "mcpServers"}:
            diagnostics.append(
                f"{config_path}: MCP component disabled: expected only $schema and mcpServers"
            )
            return {}
        if data.get("$schema") != MCP_SCHEMA_ID:
            diagnostics.append(
                f"{config_path}: MCP component disabled: unsupported or mismatched schema"
            )
            return {}
        if manifest.data.get("$schema") != PLUGIN_SCHEMA_ID:
            diagnostics.append(
                f"{config_path}: MCP component disabled: manifest schema mismatch"
            )
            return {}
        raw_servers = data.get("mcpServers")
        if not isinstance(raw_servers, dict):
            diagnostics.append(
                f"{config_path}: MCP component disabled: mcpServers must be an object"
            )
            return {}

        plugin_data = self.data_root / manifest.name
        try:
            plugin_data.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            diagnostics.append(
                f"{config_path}: MCP component disabled: cannot create PLUGIN_DATA: {exc}"
            )
            return {}
        servers: dict[str, dict[str, Any]] = {}
        validator = Draft202012Validator(MCP_SERVER_SCHEMA)
        for server_name, raw_config in raw_servers.items():
            errors = sorted(
                validator.iter_errors(raw_config), key=lambda error: list(error.path)
            )
            if errors:
                diagnostics.append(
                    f"{config_path}#{server_name}: invalid MCP server skipped: {errors[0].message}"
                )
                continue
            try:
                adapted = _adapt_mcp_server(
                    raw_config,
                    plugin_root=manifest.root,
                    plugin_data=plugin_data,
                )
            except ValueError as exc:
                diagnostics.append(
                    f"{config_path}#{server_name}: invalid MCP server skipped: {exc}"
                )
                continue
            servers[f"{manifest.name}.{server_name}"] = adapted
        return servers


def _read_json_object(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate object key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys
        )
    except json.JSONDecodeError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise ValueError("top level must be an object")
    return parsed


def _adapt_mcp_server(
    config: dict[str, Any], *, plugin_root: Path, plugin_data: Path
) -> dict[str, Any]:
    server_type = config["type"]
    if server_type == "stdio":
        command = _resolve_command(config["command"], plugin_root)
        args = [
            _expand_plugin_variables(value, plugin_root, plugin_data)
            for value in config.get("args", [])
        ]
        env = {
            key: _expand_plugin_variables(value, plugin_root, plugin_data)
            for key, value in config.get("env", {}).items()
        }
        env["PLUGIN_ROOT"] = str(plugin_root)
        env["PLUGIN_DATA"] = str(plugin_data)
        cwd = _resolve_cwd(config.get("cwd"), plugin_root, plugin_data)
        return {
            "command": command,
            "args": args,
            "env": env,
            "cwd": str(cwd),
            "transport": "stdio",
        }

    url = config["url"]
    _validate_remote_url(url)
    headers = config.get("headers", {})
    _validate_headers(headers)
    return {
        "url": url,
        "headers": dict(headers),
        "transport": server_type,
    }


def _resolve_command(command: str, plugin_root: Path) -> str:
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


def _resolve_cwd(cwd: str | None, plugin_root: Path, plugin_data: Path) -> Path:
    if cwd is None:
        return plugin_root
    expanded = _expand_plugin_variables(cwd, plugin_root, plugin_data)
    resolved = (
        (plugin_root / cwd[2:]).resolve()
        if cwd.startswith("./")
        else Path(expanded).resolve()
    )
    expected_root = plugin_data if cwd.startswith("${PLUGIN_DATA}") else plugin_root
    if not resolved.is_relative_to(expected_root):
        raise ValueError("cwd escapes its declared plugin root")
    return resolved


def _expand_plugin_variables(value: str, plugin_root: Path, plugin_data: Path) -> str:
    replacements = {
        "PLUGIN_ROOT": str(plugin_root),
        "PLUGIN_DATA": str(plugin_data),
    }
    return PLUGIN_VARIABLE_PATTERN.sub(
        lambda match: replacements[match.group(1)], value
    )


def _validate_remote_url(url: str) -> None:
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
    if parsed.scheme == "http" and not _is_loopback_host(host):
        raise ValueError("non-loopback MCP URLs must use HTTPS")


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_headers(headers: dict[str, str]) -> None:
    normalized_names: set[str] = set()
    for name, value in headers.items():
        normalized = name.casefold()
        if not HEADER_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid HTTP header name: {name}")
        if normalized in normalized_names:
            raise ValueError(f"duplicate case-insensitive HTTP header: {name}")
        if "\r" in value or "\n" in value:
            raise ValueError(f"invalid HTTP header value for {name}")
        normalized_names.add(normalized)


def _is_contained(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _tree_is_contained(path: Path, root: Path) -> bool:
    if not _is_contained(path, root):
        return False
    try:
        for current_root, directories, files in os.walk(path, followlinks=False):
            for name in [*directories, *files]:
                if not _is_contained(Path(current_root) / name, root):
                    return False
    except OSError:
        return False
    return True
