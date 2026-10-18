"""Implement ``Environment.run_code`` with Monty, a sandboxed Python subset interpreter.

Monty runs the code in a worker process and suspends it at every call that leaves the
sandbox: ``tools.<name>(...)``, ``republic`` models and file access. Bub answers each
suspension on the host and resumes the code, so tool calls go through Bub's tool executor
and hooks, as with the subprocess runner, and file access goes through the environment's
``read_text`` and ``write_text``. Concurrent calls (``asyncio.gather``) are served concurrently.
"""

from __future__ import annotations

import posixpath
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from bub.environment import CodeFailed
from pydantic_core import to_jsonable_python
from pydantic_monty import (
    NOT_HANDLED,
    AsyncMonty,
    ClassInstance,
    MontyComplete,
    MontyRuntimeError,
    MontySyntaxError,
    ResourceLimits,
)

from .republic_module import (
    from_json,
    republic_module,
    rewrite_republic_imports,
    to_json,
    to_sandbox,
)

if TYPE_CHECKING:
    from bub.environment import CallTool, Environment
    from pydantic_monty import OsHandler

type Filesystem = Literal["read-write", "read-only", "none"]

SCRIPT_NAME = "run_code.py"
UNSUPPORTED_HINT = (
    "run_code runs in Monty, a sandboxed subset of Python: no class inheritance, `match`,"
    " generators, `async with`, `async for`, `del`, method decorators or third-party packages."
    " Rewrite the code without them; use republic models without `async with`, and"
    " `await model.chat(...)` instead of `model.stream(...)`."
)


@dataclass(frozen=True)
class MontyOptions:
    """How :func:`run_code_in_monty` runs code."""

    filesystem: Filesystem = "read-write"
    """File access from the code (``open`` is not supported, use ``pathlib.Path``):
    ``Path.read_text``/``write_text`` through the environment (``read-write``), reads only
    (``read-only``), or nothing (``none``)."""

    limits: ResourceLimits = field(
        default_factory=lambda: ResourceLimits(max_suspensions=10_000)
    )
    """Monty resource limits for each run. ``max_suspensions`` counts tool calls and file access."""

    max_processes: int | None = None
    """Cap on Monty worker processes per pool; defaults to the CPU count."""


class _Tools(ClassInstance):
    """``tools`` inside the sandbox: each method call becomes ``call_tool(name, kwargs)``."""

    def __init__(self, names: Sequence[str], call_tool: CallTool) -> None:
        super().__init__(types.SimpleNamespace(), allowed_methods=names)
        self._names = frozenset(names)
        self._call_tool = call_tool

    def call_method(
        self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        if name not in self._names:
            raise AttributeError(f"unknown tool: tools.{name}")
        if args:
            raise TypeError(f"tools.{name}() accepts keyword arguments only")
        return self._call(name, kwargs)

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        # Results cross into the sandbox as plain data and republic values, as in the
        # subprocess runner.
        result = await self._call_tool(name, to_json(arguments))
        return to_sandbox(from_json(to_jsonable_python(result)))


class EnvironmentFiles:
    """An ``os=`` handler that serves ``Path.read_text``/``write_text`` from an environment.

    Other OS calls are left to Monty's default error, and the host's environment
    variables are never visible to the code.
    """

    def __init__(self, environment: Environment, filesystem: Filesystem) -> None:
        self.environment = environment
        self.filesystem = filesystem

    def __call__(
        self,
        *,
        name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        is_async: bool,
        **_: Any,
    ) -> Any:
        if name == "os.getenv":
            return args[1] if len(args) > 1 else kwargs.get("default")
        if name == "os.environ":
            return {}
        if (
            self.filesystem == "none"
            or not args
            or not isinstance(args[0], PurePosixPath)
        ):
            return NOT_HANDLED
        path = str(args[0])
        if name == "Path.read_text":
            return self.environment.read_text(path)
        if name == "Path.write_text":
            if self.filesystem != "read-write":
                raise PermissionError(f"read-only file system: {path!r}")
            return self._write_text(path, args[1])
        return NOT_HANDLED

    async def _write_text(self, path: str, content: str) -> int:
        await self.environment.write_text(path, content)
        return len(content)


async def run_code_in_monty(
    pool: AsyncMonty,
    code: str,
    *,
    tools: Sequence[str],
    call_tool: CallTool,
    write: Callable[[str], None],
    options: MontyOptions,
    os: OsHandler | None = None,
    cwd: str | None = None,
) -> None:
    """Implement ``Environment.run_code`` on a Monty worker from ``pool``.

    ``os`` answers file access and other OS calls; ``cwd`` (an absolute POSIX path) is
    where relative paths resolve. Cancelling the call stops the code and its pending
    tool calls.
    """

    def on_print(stream: Literal["stdout", "stderr"], text: str) -> None:
        if stream == "stdout":
            write(text)

    if cwd is not None and not posixpath.isabs(cwd):
        cwd = None
    republic, republic_functions = republic_module(call_tool)
    async with pool.checkout(script_name=SCRIPT_NAME, limits=options.limits) as session:
        try:
            snapshot = await session.feed_start(
                rewrite_republic_imports(code),
                inputs={"tools": _Tools(tools, call_tool), "republic": republic},
                external_lookup=republic_functions,
                print_callback=on_print,
                os=os,
                cwd=cwd,
            )
            while not isinstance(snapshot, MontyComplete):
                snapshot = await snapshot.resume_auto()
        except (MontyRuntimeError, MontySyntaxError) as exc:
            error = exc.display("type-msg")
            if isinstance(exc.exception(), NotImplementedError):
                error = f"{error}\n{UNSUPPORTED_HINT}"
            raise CodeFailed(error, exc.display("traceback")) from None
