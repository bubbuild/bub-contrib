"""Settings for the ``docker_env:`` config section (env prefix ``BUB_DOCKER_ENV_``)."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Literal

import bub
from pydantic import Field
from pydantic_settings import SettingsConfigDict

type ContainerScope = Literal["session", "shared"]


@bub.config(name="docker_env")
class DockerEnvConfig(bub.Settings):
    """Where and how sessions run their processes in Docker."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_DOCKER_ENV_",
        env_parse_none_str="null",
        extra="ignore",
    )

    sessions: str = Field(
        default="*",
        description=(
            "Comma-separated session id glob patterns that run in Docker. Session"
            " ids look like '<channel>:<chat id>', so 'qq:*' keeps only QQ chats in"
            " Docker. Other sessions keep Bub's host environment."
        ),
    )
    scope: ContainerScope = Field(
        default="session",
        description=(
            "'session' gives every session its own container; 'shared' runs all"
            " matching sessions in one container (cheaper, but sessions can see"
            " each other's processes and /tmp)."
        ),
    )
    image: str = Field(
        default="python:3.12-slim",
        description=(
            "Image to run; it needs bash, sh, setsid (util-linux), cat, mkdir,"
            " dirname and ls, and python3 for run_code."
        ),
    )
    network: str = Field(
        default="none",
        description="Docker network for the containers: 'none' (no network), 'bridge', or a named network.",
    )
    memory: str = Field(default="1g", description="Memory limit (docker --memory).")
    cpus: str = Field(default="1", description="CPU limit (docker --cpus).")
    pids_limit: int = Field(
        default=256, ge=16, description="Process limit (docker --pids-limit)."
    )
    user: str = Field(
        default="",
        description=(
            "uid:gid to run as; empty uses the host user's, so files in the"
            " workspace keep their owner; 'none' passes no --user (rootless"
            " Docker, or Podman with --userns=keep-id in extra_run_args)."
        ),
    )
    host_workspace: str = Field(
        default="",
        description=(
            "The workspace path as the Docker daemon sees it. The daemon resolves"
            " bind mounts on its own host, so set this when Bub runs in a container"
            " using the host's docker.sock, or talks to a remote Docker. Skill"
            " directories are not mounted then, since their paths do not map either."
        ),
    )
    mount_options: str = Field(
        default="",
        description="Bind mount options, e.g. 'z' to relabel for SELinux.",
    )
    extra_run_args: str = Field(
        default="",
        description=(
            "Extra `docker run` arguments, shell-quoted, e.g. '--runtime=runsc'"
            " for gVisor or '--userns=keep-id' for Podman."
        ),
    )
    workdir: str = Field(
        default="",
        description=(
            "Where the workspace is mounted in the container. Empty uses the"
            " workspace's own path, so paths Bub shows the model (its prompt"
            " names the workspace) exist in the container too."
        ),
    )
    read_only: bool = Field(
        default=True,
        description=(
            "Run with a read-only root filesystem and a tmpfs /tmp, so only the"
            " workspace and /tmp are writable. To add tools, build an image with"
            " them rather than turning this off."
        ),
    )
    tmp_size: str = Field(
        default="512m", description="Size of the /tmp tmpfs when read_only is on."
    )
    docker: str = Field(default="docker", description="The docker CLI to call.")
    mount_skills: bool = Field(
        default=True,
        description=(
            "Mount skill directories outside the workspace (~/.agents/skills and"
            " skills shipped by installed plugins) read-only at their host paths,"
            " so scripts a skill tells the model to run exist in the container."
        ),
    )

    def matches(self, session_id: str) -> bool:
        patterns = [item.strip() for item in self.sessions.split(",") if item.strip()]
        return any(fnmatchcase(session_id, pattern) for pattern in patterns)
