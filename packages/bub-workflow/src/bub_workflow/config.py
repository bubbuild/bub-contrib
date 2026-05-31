from __future__ import annotations

from pathlib import Path

import bub
from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

CONFIG_NAME = "workflow"


@bub.config(name=CONFIG_NAME)
class WorkflowSettings(bub.Settings):
    model_config = SettingsConfigDict(
        env_prefix="BUB_WORKFLOW_",
        env_file=".env",
        extra="ignore",
    )

    projection_dir: Path = Path(".bub/workflows")
    template_dirs: list[Path] = Field(
        default_factory=lambda: [Path(".bub/workflow/templates")]
    )

    @field_validator("projection_dir", mode="after")
    @classmethod
    def _normalize_projection_dir(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("template_dirs", mode="after")
    @classmethod
    def _normalize_template_dirs(cls, value: list[Path]) -> list[Path]:
        return [path.expanduser() for path in value]


def resolve_workspace_path(workspace: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    return workspace / path
