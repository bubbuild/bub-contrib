"""Bub plugin: run matching sessions' tools in a local OS sandbox."""

from __future__ import annotations

from pathlib import Path

import bub
from bub import hookimpl
from bub.environment import Environment
from loguru import logger

from .config import SandboxEnvConfig
from .environment import SandboxEnvironment


@hookimpl
def provide_environment(session_id: str, workspace: Path) -> Environment | None:
    """A sandboxed environment for sessions matching ``sandbox_env.sessions``.

    Other sessions get None, so Bub's host environment serves them.
    """

    config = bub.ensure_config(SandboxEnvConfig)
    if not config.matches(session_id):
        logger.debug("sandbox_env.session.host session_id={}", session_id)
        return None
    environment = SandboxEnvironment(config, workspace=Path(workspace))
    logger.info(
        "sandbox_env.session session_id={} backend={} network={} writable={}",
        session_id,
        environment.backend,
        config.network,
        ",".join(environment.policy.writable),
    )
    return environment
