"""Bub environments whose ``run_code`` runs in Monty.

Three ways to get one:

- :class:`MontyCodeMixin` replaces ``run_code`` in an environment class:
  ``class SandboxedMonty(MontyCodeMixin, SandboxEnvironment): ...``.
- :func:`create_monty_environment` wraps an existing environment instance and delegates
  everything but ``run_code`` to it.
- :class:`MontyEnvironment` is Bub's host environment with Monty ``run_code``; the
  plugin provides it.

``bash`` and ``fs.*`` keep running wherever the underlying environment runs them, and
file access from the code goes through the same environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bub.environment import Environment, LocalEnvironment, Process
from pydantic_monty import AsyncMonty

from .runner import EnvironmentFiles, MontyOptions, run_code_in_monty

if TYPE_CHECKING:
    from bub.environment import CallTool
    from pydantic_monty import OsHandler


class MontyCodeMixin:
    """Run ``run_code`` in Monty; mix in before an :class:`~bub.environment.Environment` class.

    Set ``monty_options`` (a class attribute or per instance) to configure it. The
    environment owns a pool of Monty workers, started on the first run and stopped by
    ``close()``. Override :meth:`monty_os_handler` to change what file access the code gets.
    """

    monty_options: MontyOptions = MontyOptions()
    _monty_pool: AsyncMonty | None = None
    _monty_pool_lock: asyncio.Lock | None = None

    async def monty_pool(self) -> AsyncMonty:
        """The environment's Monty worker pool, started on first use."""
        if self._monty_pool is None:
            if self._monty_pool_lock is None:
                self._monty_pool_lock = asyncio.Lock()
            async with self._monty_pool_lock:
                if self._monty_pool is None:
                    pool = AsyncMonty(max_processes=self.monty_options.max_processes)
                    await pool.__aenter__()
                    self._monty_pool = pool
        return self._monty_pool

    def monty_os_handler(self) -> OsHandler | None:
        """Answers OS calls from the code; defaults to file access through this environment."""
        assert isinstance(self, Environment)  # noqa: S101
        return EnvironmentFiles(self, self.monty_options.filesystem)

    async def run_code(
        self,
        code: str,
        *,
        tools: Sequence[str],
        call_tool: CallTool,
        write: Callable[[str], None],
    ) -> None:
        assert isinstance(self, Environment)  # noqa: S101
        await run_code_in_monty(
            await self.monty_pool(),
            code,
            tools=tools,
            call_tool=call_tool,
            write=write,
            options=self.monty_options,
            os=self.monty_os_handler(),
            cwd=self.workspace,
        )

    async def close(self) -> None:
        pool, self._monty_pool = self._monty_pool, None
        try:
            if pool is not None:
                await pool.__aexit__(None, None, None)
        finally:
            await super().close()  # type: ignore[misc]


class MontyEnvironment(MontyCodeMixin, LocalEnvironment):
    """Bub's host environment, with ``run_code`` in Monty instead of a Python subprocess."""

    def __init__(
        self,
        workspace: str | Path | None = None,
        *,
        options: MontyOptions | None = None,
    ) -> None:
        super().__init__(workspace)
        if options is not None:
            self.monty_options = options


class MontyWrappedEnvironment(MontyCodeMixin, Environment):
    """Delegate everything to ``environment`` except ``run_code``, which runs in Monty.

    Attributes the wrapper does not define (for example a backend-specific ``read_bytes``)
    are read from the wrapped environment. ``close()`` closes the wrapped environment too.
    """

    def __init__(
        self, environment: Environment, *, options: MontyOptions | None = None
    ) -> None:
        self.environment = environment
        if options is not None:
            self.monty_options = options

    @property  # type: ignore[override]
    def workspace(self) -> str | None:
        return self.environment.workspace

    @property  # type: ignore[override]
    def python(self) -> str:
        return self.environment.python

    async def spawn(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Process:
        return await self.environment.spawn(command, cwd=cwd, env=env)

    async def read_text(self, path: str) -> str:
        return await self.environment.read_text(path)

    async def write_text(self, path: str, content: str) -> None:
        await self.environment.write_text(path, content)

    def resolve_path(self, path: str) -> str:
        return self.environment.resolve_path(path)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.environment.close()

    def __getattr__(self, name: str) -> Any:
        if name == "environment":
            raise AttributeError(name)
        return getattr(self.environment, name)


def create_monty_environment(
    environment: Environment, options: MontyOptions | None = None
) -> Environment:
    """Wrap ``environment`` so its ``run_code`` runs in Monty; everything else is delegated.

    The wrapper owns ``environment``: closing the wrapper closes it.
    """
    return MontyWrappedEnvironment(environment, options=options)
