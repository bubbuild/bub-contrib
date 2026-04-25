from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_config_path() -> Path:
    from bub.builtin.settings import load_settings

    return load_settings().home / "acp.json"


class ACPAgentProcessConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


class ACPConfig(BaseModel):
    default_agent: str | None = Field(default=None, alias="defaultAgent")
    agents: dict[str, ACPAgentProcessConfig] = Field(default_factory=dict)


class ACPSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_ACP_", extra="ignore")

    config_path: Path = Field(default_factory=default_config_path)

    def read_config(self) -> ACPConfig:
        if not self.config_path.exists():
            return ACPConfig()
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError("ACP config file must contain a top-level mapping")
        return ACPConfig.model_validate(raw)

    def write_config(self, config: ACPConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = config.model_dump(mode="json", by_alias=True, exclude_none=True)
        self.config_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def list_agents(self) -> dict[str, ACPAgentProcessConfig]:
        return self.read_config().agents

    def default_agent_name(self) -> str | None:
        return self.read_config().default_agent

    def default_agent(self) -> ACPAgentProcessConfig | None:
        config = self.read_config()
        if config.default_agent is None:
            return None
        return config.agents.get(config.default_agent)

    def upsert_agent(
        self,
        name: str,
        agent: ACPAgentProcessConfig,
        *,
        make_default: bool = False,
    ) -> ACPConfig:
        config = self.read_config()
        config.agents[name] = agent
        if make_default or config.default_agent is None:
            config.default_agent = name
        self.write_config(config)
        return config

    def remove_agent(self, name: str) -> ACPConfig:
        config = self.read_config()
        if name not in config.agents:
            raise KeyError(name)
        config.agents.pop(name)
        if config.default_agent == name:
            config.default_agent = next(iter(config.agents), None)
        self.write_config(config)
        return config

    def use_agent(self, name: str) -> ACPConfig:
        config = self.read_config()
        if name not in config.agents:
            raise KeyError(name)
        config.default_agent = name
        self.write_config(config)
        return config
