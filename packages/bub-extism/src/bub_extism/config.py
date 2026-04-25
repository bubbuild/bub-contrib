from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_config_path() -> Path:
    from bub.builtin.settings import load_settings

    return load_settings().home / "extism.json"


class ExtismHookMap(BaseModel):
    resolve_session: str | None = None
    build_prompt: str | None = None
    run_model: str | None = None
    run_model_stream: str | None = None
    load_state: str | None = None
    save_state: str | None = None
    render_outbound: str | None = None
    dispatch_outbound: str | None = None
    register_cli_commands: str | None = None
    onboard_config: str | None = None
    on_error: str | None = None
    system_prompt: str | None = None
    provide_tape_store: str | None = None
    provide_channels: str | None = None
    build_tape_context: str | None = None


class ExtismPluginConfig(BaseModel):
    manifest: dict[str, Any] | None = None
    wasm_path: Path | None = Field(default=None, alias="wasmPath")
    wasm_url: str | None = Field(default=None, alias="wasmUrl")
    hooks: ExtismHookMap = Field(default_factory=ExtismHookMap)
    config: dict[str, str] = Field(default_factory=dict)
    wasi: bool = False

    @model_validator(mode="after")
    def validate_wasm_source(self) -> ExtismPluginConfig:
        sources = [
            self.manifest is not None,
            self.wasm_path is not None,
            self.wasm_url is not None,
        ]
        if sum(sources) != 1:
            raise ValueError("exactly one of manifest, wasmPath, or wasmUrl is required")
        return self

    def plugin_input(self) -> dict[str, Any] | bytes:
        if self.manifest is not None:
            return self.manifest
        if self.wasm_url is not None:
            return {"wasm": [{"url": self.wasm_url}]}
        if self.wasm_path is None:
            raise RuntimeError("wasmPath is required")
        return self.wasm_path.expanduser().read_bytes()


class ExtismConfig(BaseModel):
    default_plugin: str | None = Field(default=None, alias="defaultPlugin")
    plugins: dict[str, ExtismPluginConfig] = Field(default_factory=dict)

    def selected_plugin(self) -> ExtismPluginConfig | None:
        if self.default_plugin is None:
            return None
        return self.plugins.get(self.default_plugin)


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
