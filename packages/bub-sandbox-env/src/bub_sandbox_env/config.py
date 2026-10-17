"""Settings for the ``sandbox_env:`` config section (env prefix ``BUB_SANDBOX_ENV_``)."""

from __future__ import annotations

import sys
from fnmatch import fnmatchcase
from typing import Literal

import bub
from pydantic import Field
from pydantic_settings import SettingsConfigDict

type Backend = Literal["auto", "seatbelt", "bwrap"]

DEFAULT_DENY_READ = [
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.azure",
    "~/.config/gcloud",
    "~/.kube",
    "~/.docker",
    "~/.netrc",
    "~/.git-credentials",
    "~/.npmrc",
    "~/.pypirc",
    "$BUB_HOME",
]
DEFAULT_ENV_DENY = [
    "BUB_*",
    "*_API_KEY",
    "*_TOKEN",
    "*_SECRET",
    "*_SECRET_KEY",
    "*_PASSWORD",
    "AWS_*",
]


@bub.config(name="sandbox_env")
class SandboxEnvConfig(bub.Settings):
    """Which sessions run sandboxed and what the sandbox lets them do."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_SANDBOX_ENV_",
        env_parse_none_str="null",
        extra="ignore",
    )

    sessions: list[str] = Field(
        default_factory=lambda: ["*"],
        description=(
            "Session id glob patterns that run sandboxed. Session"
            " ids look like '<channel>:<chat id>', so 'qq:*' sandboxes only QQ"
            " chats. Other sessions keep Bub's host environment."
        ),
    )
    backend: Backend = Field(
        default="auto",
        description=(
            "'seatbelt' (macOS sandbox-exec), 'bwrap' (Linux bubblewrap), or"
            " 'auto' to pick the one for this platform."
        ),
    )
    network: bool = Field(
        default=False,
        description="Allow network access. Off blocks every outbound and inbound connection.",
    )
    writable: list[str] = Field(
        default_factory=list,
        description=(
            "Extra paths the sandbox may write, besides the"
            " workspace and the session's temp directory. '~' expands."
        ),
    )
    deny_read: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DENY_READ),
        description=(
            "Paths the sandbox may not read: credentials and Bub's"
            " own home (config with API keys). '~' and '$BUB_HOME' expand."
            " The more specific path wins: a writable path inside a denied one"
            " stays usable, a denied path inside a writable one stays denied."
        ),
    )
    env_deny: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ENV_DENY),
        description=(
            "Glob patterns of environment variable names (case"
            " insensitive) not passed to sandboxed processes, so API keys and"
            " tokens in Bub's environment do not leak into commands."
        ),
    )
    bwrap: str = Field(default="bwrap", description="The bubblewrap executable.")

    def matches(self, session_id: str) -> bool:
        return any(fnmatchcase(session_id, pattern) for pattern in self.sessions)

    def resolved_backend(self) -> Literal["seatbelt", "bwrap"]:
        if self.backend != "auto":
            return self.backend
        return "seatbelt" if sys.platform == "darwin" else "bwrap"
