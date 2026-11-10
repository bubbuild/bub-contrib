"""Bub plugin: run matching sessions' tools in Docker containers."""

from __future__ import annotations

from pathlib import Path

import bub
from bub import hookimpl
from bub.environment import Environment
from loguru import logger

from .config import DockerEnvConfig
from .environment import DockerEnvironment


@hookimpl
def provide_environment(session_id: str, workspace: Path) -> Environment | None:
    """A Docker environment for sessions matching ``docker_env.sessions``.

    Other sessions get None, so Bub's host environment serves them.
    """

    config = bub.ensure_config(DockerEnvConfig)
    if not config.matches(session_id):
        logger.debug("docker_env.session.host session_id={}", session_id)
        return None
    environment = DockerEnvironment(
        config, session_id=session_id, host_workspace=Path(workspace)
    )
    logger.info(
        "docker_env.session session_id={} container={} scope={}",
        session_id,
        environment.container.name,
        config.scope,
    )
    return environment
