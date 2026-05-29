from __future__ import annotations

import asyncio
import shlex
from pathlib import Path

import typer

from bub.channels.message import ChannelMessage
from bub.envelope import field_of
from bub.framework import BubFramework
from bub_dynamic_workflows.state import WorkflowProjectionStore

app = typer.Typer(name="workflow", help="Run bee-on-tape workflows.", add_completion=False)


@app.command("start")
def start(
    ctx: typer.Context,
    spec_path: Path,
    args: Path | None = typer.Option(None, "--args", help="Workflow args JSON."),
    run_id: str | None = typer.Option(None, "--run-id", help="Optional run id."),
    session_id: str = typer.Option("workflow:cli", "--session-id", help="Bub session id."),
) -> None:
    fields = ["workflow.start", f"spec_path={spec_path}"]
    if args is not None:
        fields.append(f"args_path={args}")
    if run_id is not None:
        fields.append(f"run_id={run_id}")
    _run_bub_command(ctx, session_id=session_id, command=_command(fields))


@app.command("resume")
def resume(
    ctx: typer.Context,
    run_id: str,
    session_id: str = typer.Option("workflow:cli", "--session-id", help="Bub session id."),
) -> None:
    _run_bub_command(ctx, session_id=session_id, command=_command(["workflow.resume", f"run_id={run_id}"]))


@app.command("cancel")
def cancel(
    ctx: typer.Context,
    run_id: str,
    session_id: str = typer.Option("workflow:cli", "--session-id", help="Bub session id."),
) -> None:
    _run_bub_command(ctx, session_id=session_id, command=_command(["workflow.cancel", f"run_id={run_id}"]))


@app.command("status")
def status(
    ctx: typer.Context,
    run_id: str,
    workspace: Path | None = typer.Option(None, "--workspace", "-w", help="Workspace path."),
) -> None:
    framework = ctx.ensure_object(BubFramework)
    root = workspace.expanduser().resolve() if workspace is not None else framework.workspace
    typer.echo(WorkflowProjectionStore(root).read(run_id).model_dump_json(indent=2))


def register_cli_commands(root: typer.Typer) -> None:
    root.add_typer(app)


def _command(fields: list[str]) -> str:
    return "," + " ".join(shlex.quote(field) for field in fields)


def _run_bub_command(ctx: typer.Context, *, session_id: str, command: str) -> None:
    framework = ctx.ensure_object(BubFramework)
    inbound = ChannelMessage(
        session_id=session_id,
        channel="cli",
        chat_id="workflow",
        content=command,
        context={"sender_id": "human"},
    )

    async def execute() -> None:
        async with framework.running():
            result = await framework.process_inbound(inbound)
        for outbound in result.outbounds:
            typer.echo(str(field_of(outbound, "content", "")))

    asyncio.run(execute())
