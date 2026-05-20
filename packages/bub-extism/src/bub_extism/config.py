from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLUGIN_HOOK_NAMES = (
    "resolve_session",
    "build_prompt",
    "run_model",
    "run_model_stream",
    "load_state",
    "save_state",
    "render_outbound",
    "dispatch_outbound",
    "onboard_config",
    "on_error",
    "system_prompt",
    "provide_tape_store",
    "provide_channels",
    "build_tape_context",
)
CLI_HOOK_NAME = "register_cli_commands"
ALLOWED_HOOK_NAMES = frozenset((*PLUGIN_HOOK_NAMES, CLI_HOOK_NAME))


def default_config_path() -> Path:
    from bub.builtin.settings import load_settings

    return load_settings().home / "extism.json"


def normalize_hook_bindings(hooks: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for hook_name, export_name in hooks.items():
        hook_text = str(hook_name).strip()
        export_text = str(export_name).strip()
        if hook_text not in ALLOWED_HOOK_NAMES:
            supported = ", ".join(sorted(ALLOWED_HOOK_NAMES))
            raise ValueError(f"unsupported hook '{hook_text}'; expected one of: {supported}")
        if not export_text:
            raise ValueError(f"hook '{hook_text}' requires a non-empty export name")
        normalized[hook_text] = export_text
    return normalized


class ExtismPluginConfig(BaseModel):
    manifest: dict[str, Any]
    hooks: dict[str, str] = Field(default_factory=dict)
    wasi: bool = False

    @field_validator("hooks")
    @classmethod
    def validate_hooks(cls, hooks: dict[str, str]) -> dict[str, str]:
        return normalize_hook_bindings(hooks)


class ExtismConfig(BaseModel):
    plugins: dict[str, ExtismPluginConfig] = Field(default_factory=dict)


class ExtismSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_EXTISM_", extra="ignore")

    config_path: Path = Field(default_factory=default_config_path)

    def read_config(self) -> ExtismConfig:
        if not self.config_path.exists():
            return ExtismConfig()
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError("Extism config file must contain a top-level mapping")
        return ExtismConfig.model_validate(raw)

    def write_config(self, config: ExtismConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = config.model_dump(mode="json")
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
