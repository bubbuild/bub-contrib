"""Bub plugin: run matching sessions' code-mode code in Monty."""

from __future__ import annotations

from pathlib import Path

import bub
from bub import hookimpl
from bub.environment import Environment
from loguru import logger

from .config import MontyEnvConfig
from .environment import MontyEnvironment


@hookimpl
def provide_environment(session_id: str, workspace: Path) -> Environment | None:
    """A host environment with Monty ``run_code`` for sessions matching ``monty_env.sessions``.

    Other sessions get None, so Bub's host environment serves them.
    """

    config = bub.ensure_config(MontyEnvConfig)
    if not config.matches(session_id):
        logger.debug("monty_env.session.host session_id={}", session_id)
        return None
    logger.info(
        "monty_env.session session_id={} filesystem={}", session_id, config.filesystem
    )
    return MontyEnvironment(Path(workspace), options=config.options())
