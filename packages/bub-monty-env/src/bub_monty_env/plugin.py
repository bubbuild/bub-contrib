"""Bub plugin: run matching sessions' code-mode code in Monty."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING

import bub
from bub import hookimpl
from bub.environment import Environment, LocalEnvironment
from loguru import logger

from .config import MontyEnvConfig
from .environment import create_monty_environment

if TYPE_CHECKING:
    from bub.framework import BubFramework


class MontyEnvPlugin:
    """Wrap the environment other plugins provide so its ``run_code`` runs in Monty."""

    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework

    async def other_environment(self, session_id: str, workspace: Path) -> Environment:
        """The environment the other plugins provide for the session, or a host one."""
        provide = self.framework.plugin_manager.subset_hook_caller(
            "provide_environment", remove_plugins=[self]
        )
        environment = provide(session_id=session_id, workspace=workspace)
        if inspect.isawaitable(environment):
            environment = await environment
        if environment is None:
            return LocalEnvironment(workspace)
        if not isinstance(environment, Environment):
            raise TypeError("hook.provide_environment must return Environment or None")
        return environment

    @hookimpl(tryfirst=True)
    async def provide_environment(
        self, session_id: str, workspace: Path
    ) -> Environment | None:
        """The other plugins' environment with Monty ``run_code``, for sessions matching ``monty_env.sessions``.

        Other sessions get None, so the other plugins serve them unchanged.
        """

        config = bub.ensure_config(MontyEnvConfig)
        if not config.matches(session_id):
            logger.debug("monty_env.session.skip session_id={}", session_id)
            return None
        environment = await self.other_environment(session_id, Path(workspace))
        logger.info(
            "monty_env.session session_id={} wraps={} filesystem={}",
            session_id,
            type(environment).__name__,
            config.filesystem,
        )
        return create_monty_environment(environment, config.options())
