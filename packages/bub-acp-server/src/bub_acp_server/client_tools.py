from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from typing import Literal

from acp.helpers import plan_entry, update_plan as acp_update_plan
from acp.interfaces import Client
from acp.schema import (
    ClientCapabilities,
    TerminalOutputResponse,
    WaitForTerminalExitResponse,
)
from pydantic import BaseModel, Field
from bub.tools import REGISTRY, Tool, ToolContext, tool

_TOOL_NAMES = (
    "bash",
    "bash.output",
    "bash.kill",
    "fs.read",
    "fs.write",
    "fs.edit",
    "tape.handoff",
    "update_plan",
)
type TerminalObserver = Callable[[str, str, str], Awaitable[None]]
type PlanStatus = Literal["pending", "in_progress", "completed"]
type PlanPriority = Literal["high", "medium", "low"]


class PlanItem(BaseModel):
    step: str = Field(min_length=1)
    status: PlanStatus
    priority: PlanPriority = "medium"


class PlanInput(BaseModel):
    explanation: str | None = None
    plan: list[PlanItem]


class ACPClientToolRuntime:
    def __init__(self) -> None:
        self._client: Client | None = None
        self._capabilities = ClientCapabilities()
        self._terminal_observer: TerminalObserver | None = None

    def connect(self, client: Client) -> None:
        self._client = client

    def set_capabilities(self, capabilities: ClientCapabilities | None) -> None:
        self._capabilities = capabilities or ClientCapabilities()

    def set_terminal_observer(self, observer: TerminalObserver | None) -> None:
        self._terminal_observer = observer

    async def read_file(
        self,
        path: str,
        offset: int,
        limit: int | None,
        context: ToolContext,
    ) -> str:
        client = self._require_client()
        fs_capabilities = self._capabilities.fs
        if fs_capabilities is None or not fs_capabilities.read_text_file:
            raise RuntimeError("ACP client does not support fs/read_text_file")
        resolved_path = _resolve_path(context, path)
        response = await client.read_text_file(
            path=str(resolved_path),
            session_id=_session_id(context),
            line=max(0, offset) + 1,
            limit=limit,
        )
        return response.content

    async def write_file(
        self,
        path: str,
        content: str,
        context: ToolContext,
    ) -> str:
        client = self._require_client()
        fs_capabilities = self._capabilities.fs
        if fs_capabilities is None or not fs_capabilities.write_text_file:
            raise RuntimeError("ACP client does not support fs/write_text_file")
        resolved_path = _resolve_path(context, path)
        await client.write_text_file(
            content=content,
            path=str(resolved_path),
            session_id=_session_id(context),
        )
        return f"wrote: {resolved_path}"

    async def edit_file(
        self,
        path: str,
        old: str,
        new: str,
        start: int,
        context: ToolContext,
    ) -> str:
        resolved_path = _resolve_path(context, path)
        text = await self.read_file(path, 0, None, context)
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        previous = "\n".join(lines[:start])
        to_replace = "\n".join(lines[start:])
        if old not in to_replace:
            raise ValueError(f"{old!r} not found in {resolved_path} from line {start}")
        replaced = to_replace.replace(old, new)
        if previous:
            replaced = previous + "\n" + replaced
        if trailing_newline:
            replaced += "\n"
        await self.write_file(path, replaced, context)
        return f"edited: {resolved_path}"

    async def bash(
        self,
        cmd: str,
        cwd: str | None,
        timeout_seconds: int,
        background: bool,
        context: ToolContext,
    ) -> str:
        client = self._require_terminal_client()
        session_id = _session_id(context)
        target_cwd = _resolve_cwd(context, cwd)
        terminal = await client.create_terminal(
            command="bash",
            args=["-lc", cmd],
            cwd=str(target_cwd),
            session_id=session_id,
        )
        if self._terminal_observer is not None:
            await self._terminal_observer(session_id, cmd, terminal.terminal_id)
        if background:
            return f"started: {terminal.terminal_id}"

        try:
            try:
                async with asyncio.timeout(timeout_seconds):
                    status = await client.wait_for_terminal_exit(
                        session_id=session_id,
                        terminal_id=terminal.terminal_id,
                    )
            except asyncio.CancelledError:
                await client.kill_terminal(
                    session_id=session_id,
                    terminal_id=terminal.terminal_id,
                )
                raise
            except TimeoutError:
                await client.kill_terminal(
                    session_id=session_id,
                    terminal_id=terminal.terminal_id,
                )
                return (
                    f"command timed out after {timeout_seconds} seconds "
                    "and was terminated"
                )

            output = await client.terminal_output(
                session_id=session_id,
                terminal_id=terminal.terminal_id,
            )
            _raise_for_failed_terminal(status, output.output)
            return output.output.strip() or "(no output)"
        finally:
            with contextlib.suppress(Exception):
                await client.release_terminal(
                    session_id=session_id,
                    terminal_id=terminal.terminal_id,
                )

    async def bash_output(
        self,
        shell_id: str,
        offset: int,
        limit: int | None,
        context: ToolContext,
    ) -> str:
        client = self._require_terminal_client()
        response = await client.terminal_output(
            session_id=_session_id(context), terminal_id=shell_id
        )
        output = response.output
        start = max(0, min(offset, len(output)))
        end = len(output) if limit is None else min(len(output), start + max(0, limit))
        body = output[start:end].rstrip() or "(no output)"
        return _render_terminal_output(shell_id, response, end, body)

    async def kill_bash(self, shell_id: str, context: ToolContext) -> str:
        client = self._require_terminal_client()
        session_id = _session_id(context)
        await client.kill_terminal(session_id=session_id, terminal_id=shell_id)
        try:
            response = await client.terminal_output(
                session_id=session_id, terminal_id=shell_id
            )
            exit_code = _terminal_exit_code(response)
            return f"id: {shell_id}\nstatus: exited\nexit_code: {exit_code}"
        finally:
            await client.release_terminal(session_id=session_id, terminal_id=shell_id)

    async def update_plan(
        self,
        request: PlanInput,
        context: ToolContext,
    ) -> str:
        plan = request.plan
        in_progress = sum(item.status == "in_progress" for item in plan)
        if in_progress > 1:
            raise ValueError("plan must contain at most one in_progress step")

        entries = [
            plan_entry(
                item.step,
                priority=item.priority,
                status=item.status,
            )
            for item in plan
        ]
        payload: dict[str, object] = {
            "entries": [
                {
                    "content": item.step,
                    "priority": item.priority,
                    "status": item.status,
                }
                for item in plan
            ]
        }
        if request.explanation:
            payload["explanation"] = request.explanation

        await context.tape.append_event("plan", payload, run_id=context.run_id)
        await self._require_client().session_update(
            _session_id(context), acp_update_plan(entries)
        )
        return f"Plan updated with {len(plan)} steps"

    @staticmethod
    async def tape_handoff(
        name: str,
        summary: str,
        context: ToolContext,
    ) -> str:
        await context.tape.handoff(name=name, state={"summary": summary})
        return f"anchor added: {name}"

    def _require_client(self) -> Client:
        if self._client is None:
            raise RuntimeError("ACP client is not connected")
        return self._client

    def _require_terminal_client(self) -> Client:
        client = self._require_client()
        if not self._capabilities.terminal:
            raise RuntimeError("ACP client does not support terminal methods")
        return client


@contextmanager
def replace_builtin_tools(runtime: ACPClientToolRuntime) -> Generator[None]:
    import_module("bub.builtin.tools")
    originals = {name: REGISTRY.get(name) for name in _TOOL_NAMES}
    _register_replacements(runtime)
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is None:
                REGISTRY.pop(name, None)
            else:
                REGISTRY[name] = original


def _register_replacements(runtime: ACPClientToolRuntime) -> dict[str, Tool]:
    @tool(name="bash", context=True)
    async def bash(
        cmd: str,
        cwd: str | None = None,
        timeout_seconds: int = 30,
        background: bool = False,
        *,
        context: ToolContext,
    ) -> str:
        """Run a shell command through the ACP client terminal."""
        return await runtime.bash(cmd, cwd, timeout_seconds, background, context)

    @tool(name="bash.output", context=True)
    async def bash_output(
        shell_id: str,
        offset: int = 0,
        limit: int | None = None,
        *,
        context: ToolContext,
    ) -> str:
        """Read buffered output from an ACP client terminal."""
        return await runtime.bash_output(shell_id, offset, limit, context)

    @tool(name="bash.kill", context=True)
    async def kill_bash(shell_id: str, *, context: ToolContext) -> str:
        """Terminate an ACP client terminal process."""
        return await runtime.kill_bash(shell_id, context)

    @tool(name="fs.read", context=True)
    async def fs_read(
        path: str,
        offset: int = 0,
        limit: int | None = None,
        *,
        context: ToolContext,
    ) -> str:
        """Read a text file through the ACP client filesystem."""
        return await runtime.read_file(path, offset, limit, context)

    @tool(name="fs.write", context=True)
    async def fs_write(
        path: str,
        content: str,
        *,
        context: ToolContext,
    ) -> str:
        """Write a text file through the ACP client filesystem."""
        return await runtime.write_file(path, content, context)

    @tool(name="fs.edit", context=True)
    async def fs_edit(
        path: str,
        old: str,
        new: str,
        start: int = 0,
        *,
        context: ToolContext,
    ) -> str:
        """Edit a text file through the ACP client filesystem."""
        return await runtime.edit_file(path, old, new, start, context)

    @tool(name="update_plan", context=True, model=PlanInput)
    async def update_plan_tool(
        request: PlanInput,
        *,
        context: ToolContext,
    ) -> str:
        """Replace the ACP session plan and persist it to the current tape."""
        return await runtime.update_plan(request, context)

    @tool(name="tape.handoff", context=True)
    async def tape_handoff(
        name: str = "handoff",
        summary: str = "",
        *,
        context: ToolContext,
    ) -> str:
        """Compact the current context by adding a handoff anchor."""
        return await runtime.tape_handoff(name, summary, context)

    return {
        tool_item.name: tool_item
        for tool_item in (
            bash,
            bash_output,
            kill_bash,
            fs_read,
            fs_write,
            fs_edit,
            tape_handoff,
            update_plan_tool,
        )
    }


def _session_id(context: ToolContext) -> str:
    value = context.state.get("session_id")
    if value is None or not str(value).strip():
        raise RuntimeError("Bub tool context does not contain a session id")
    _, separator, session_id = str(value).partition(":")
    if not separator or not session_id:
        raise RuntimeError("Bub tool context does not contain an ACP session id")
    return session_id


def _resolve_path(context: ToolContext, path: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = _workspace(context) / candidate
    return candidate.resolve()


def _resolve_cwd(context: ToolContext, cwd: str | None) -> Path:
    return _resolve_path(context, cwd) if cwd else _workspace(context)


def _workspace(context: ToolContext) -> Path:
    value = context.state.get("_runtime_workspace")
    if value is None:
        return Path.cwd().resolve()
    return Path(str(value)).expanduser().resolve()


def _raise_for_failed_terminal(
    status: WaitForTerminalExitResponse, output: str
) -> None:
    if status.exit_code in (None, 0) and status.signal is None:
        return
    body = output.strip() or "(no output)"
    outcome = (
        f"signal {status.signal}"
        if status.signal is not None
        else f"code {status.exit_code}"
    )
    raise RuntimeError(f"command exited with {outcome}\noutput:\n{body}")


def _terminal_exit_code(response: TerminalOutputResponse) -> str:
    if response.exit_status is None or response.exit_status.exit_code is None:
        return "null"
    return str(response.exit_status.exit_code)


def _render_terminal_output(
    shell_id: str,
    response: TerminalOutputResponse,
    next_offset: int,
    body: str,
) -> str:
    status = "exited" if response.exit_status is not None else "running"
    return (
        f"id: {shell_id}\n"
        f"status: {status}\n"
        f"exit_code: {_terminal_exit_code(response)}\n"
        f"next_offset: {next_offset}\n"
        f"output:\n{body}"
    )
