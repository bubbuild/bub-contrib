"""Settings for the ``monty_env:`` config section (env prefix ``BUB_MONTY_ENV_``)."""

from __future__ import annotations

from fnmatch import fnmatchcase

import bub
from pydantic import Field
from pydantic_monty import ResourceLimits
from pydantic_settings import SettingsConfigDict

from .runner import Filesystem, MontyOptions


@bub.config(name="monty_env")
class MontyEnvConfig(bub.Settings):
    """Which sessions run code-mode code in Monty, and with what limits."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_MONTY_ENV_",
        env_parse_none_str="null",
        extra="ignore",
    )

    sessions: list[str] = Field(
        default_factory=lambda: ["*"],
        description=(
            "Session id glob patterns whose run_code runs in Monty. Session ids look"
            " like '<channel>:<chat id>', so 'qq:*' matches only QQ chats. Other"
            " sessions keep the environment other plugins provide, unwrapped."
        ),
    )
    filesystem: Filesystem = Field(
        default="read-write",
        description=(
            "File access from the code through pathlib.Path read_text/write_text:"
            " 'read-write', 'read-only' or 'none'. Paths are the same as fs.* sees."
        ),
    )
    max_memory_mb: int | None = Field(
        default=256, description="Heap limit per run in MiB; null for no limit."
    )
    max_recursion_depth: int = Field(
        default=1000, description="Maximum call stack depth."
    )
    max_suspensions: int = Field(
        default=10_000,
        description="Maximum tool calls, file accesses and other host round trips per run.",
    )
    max_processes: int | None = Field(
        default=None,
        description="Monty worker processes per session; null for the CPU count.",
    )

    def matches(self, session_id: str) -> bool:
        return any(fnmatchcase(session_id, pattern) for pattern in self.sessions)

    def options(self) -> MontyOptions:
        limits = ResourceLimits(
            max_recursion_depth=self.max_recursion_depth,
            max_suspensions=self.max_suspensions,
        )
        if self.max_memory_mb is not None:
            limits["max_memory"] = self.max_memory_mb * 1024 * 1024
        return MontyOptions(
            filesystem=self.filesystem, limits=limits, max_processes=self.max_processes
        )
