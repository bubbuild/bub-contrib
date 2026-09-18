"""Run controlled ACP MCP ablations in fresh processes with real MCP children.

The parent orchestrates runs and writes raw observations. Mutations below affect
only each short-lived probe process; production package files are never edited.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import platform
import random
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

VARIANTS = (
    "full",
    "capabilities_only",
    "registry_only",
    "shared_agent",
    "no_replace_cleanup",
    "no_shutdown_cleanup",
    "context_exit_only",
)
TOOL = "mcp.shared_identify"
MODEL_PREFIX = "mcp_shared_"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def observe_processes(records: list[dict], grace: float = 1) -> list[int]:
    deadline = time.monotonic() + grace
    while True:
        living = [record["pid"] for record in records if alive(record["pid"])]
        if not living or time.monotonic() >= deadline:
            return living
        await asyncio.sleep(0.05)


async def probe(variant: str, run_dir: Path) -> dict:
    # Set the environment before importing Bub/configuration modules.
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    os.environ["BUB_HOME"] = str(run_dir / "bub-home")
    os.chdir(workspace)

    import bub
    from acp.schema import EnvVariable, McpServerStdio, TextContentBlock
    from bub.builtin.agent import Agent
    from bub.builtin.model_runner import ModelRunner
    from bub.framework import BubFramework
    from bub.streaming import AsyncStreamEvents, StreamEvent
    from bub.tools import REGISTRY
    from bub_mcp.plugin import MCPChannel

    from bub_acp_server import agent as agent_module
    from bub_acp_server.agent import BubACPAgent
    from bub_acp_server.plugin import ACPServerPlugin

    class Client:
        def __init__(self):
            self.updates = []

        async def session_update(self, session_id, update, **kwargs):
            self.updates.append((session_id, update))

    model_observations = []

    def model_probe(self, *, tools, **kwargs):
        model_observations.append(
            sorted(t.name for t in tools if t.name.startswith(MODEL_PREFIX))
        )

        async def events():
            yield StreamEvent("text", {"delta": "schema observed"})
            yield StreamEvent("final", {"text": "schema observed", "ok": True})

        return AsyncStreamEvents(events())

    original_connect = agent_module.connect_session_mcp
    original_build = BubACPAgent._build_inbound
    original_replace = BubACPAgent._replace_session_mcp
    original_shutdown = BubACPAgent.shutdown
    original_stop = MCPChannel.stop
    channels = []
    clients = []

    async def connect(configs):
        if variant == "capabilities_only":
            return None  # Keep capability advertisement, remove only the MCP bridge.
        channel = await original_connect(configs)
        if channel is not None:
            channels.append(channel)
            clients.extend(state.client for state in channel.list().values())
        return channel

    @bub.hookimpl(specname="load_state", trylast=True)
    def registry_binding(self, message):
        if message._mcp_channel is not None:
            REGISTRY.update(message._mcp_channel.tools)

    def shared_agent(self, prompt, session):
        if self._runtime_agents:
            self._runtime_agents[session.session_id] = next(
                iter(self._runtime_agents.values())
            )
        return original_build(self, prompt, session)

    async def context_exit_only(client):
        await client.__aexit__(None, None, None)

    async def noop():
        pass

    async def replace_without_cleanup(self, session, servers):
        previous = self._mcp_channels.get(session.session_id)
        if previous is None:
            return await original_replace(self, session, servers)
        with patch.object(previous, "stop", noop):
            return await original_replace(self, session, servers)

    async def shutdown_without_cleanup(self):
        with contextlib.ExitStack() as stack:
            for channel in self._mcp_channels.values():
                stack.enter_context(patch.object(channel, "stop", noop))
            await original_shutdown(self)

    def config(label):
        return McpServerStdio(
            name="shared",
            command=sys.executable,
            args=[str(Path(__file__).with_name("mcp_ablation_server.py").resolve())],
            env=[
                EnvVariable(name="ABLATION_LABEL", value=label),
                EnvVariable(
                    name="ABLATION_PID_FILE", value=str(run_dir / f"{label}.pid.json")
                ),
            ],
        )

    result = {"variant": variant, "calls": [], "isolation_checks": []}
    with contextlib.ExitStack() as patches:
        patches.enter_context(patch.object(ModelRunner, "run", model_probe))
        patches.enter_context(
            patch.object(agent_module, "connect_session_mcp", connect)
        )
        if variant == "registry_only":
            patches.enter_context(
                patch.object(
                    ACPServerPlugin, "bind_session_mcp_tools", registry_binding
                )
            )
        elif variant == "shared_agent":
            patches.enter_context(
                patch.object(BubACPAgent, "_build_inbound", shared_agent)
            )
        elif variant == "no_replace_cleanup":
            patches.enter_context(
                patch.object(
                    BubACPAgent, "_replace_session_mcp", replace_without_cleanup
                )
            )
        elif variant == "no_shutdown_cleanup":
            patches.enter_context(
                patch.object(BubACPAgent, "shutdown", shutdown_without_cleanup)
            )

        if variant == "context_exit_only":
            patches.enter_context(
                patch.object(
                    MCPChannel, "_close_client", staticmethod(context_exit_only)
                )
            )

        framework = BubFramework(config_file=run_dir / "config.yml")
        framework.workspace = workspace
        framework.load_builtin_hooks()
        framework.plugin_manager.register(ACPServerPlugin(framework), name="acp-server")
        initial_registry = REGISTRY.copy()
        ordinary_before = Agent(framework)
        agent = BubACPAgent(framework)
        client = Client()
        agent.on_connect(client)

        async def schema(session_id, label):
            await agent.prompt(
                session_id=session_id,
                prompt=[TextContentBlock(text="Observe available tools.")],
            )
            tools = model_observations[-1]
            allowed = (
                set()
                if label == "empty"
                else {"mcp_shared_identify", f"mcp_shared_only_{label}"}
            )
            result["isolation_checks"].append(
                {
                    "session": label,
                    "visible": tools,
                    "unexpected": sorted(set(tools) - allowed),
                    "passed": not (set(tools) - allowed),
                }
            )

        async def call(session_id, expected):
            before = len(client.updates)
            observation = {"expected": expected, "passed": False}
            try:
                await agent.prompt(
                    session_id=session_id, prompt=[TextContentBlock(text=f",{TOOL}")]
                )
                text = "".join(
                    update.content.text
                    for sid, update in client.updates[before:]
                    if sid == session_id
                    and update.session_update == "agent_message_chunk"
                )
                payload = json.loads(text)
                observation.update(
                    returned=payload,
                    passed=(
                        payload["label"] == expected
                        and payload["cwd"] == str(workspace)
                    ),
                )
            except Exception as error:
                observation["error"] = f"{type(error).__name__}: {error}"
            result["calls"].append(observation)

        try:
            async with framework.running():
                caps = (await agent.initialize(1)).agent_capabilities.mcp_capabilities
                result["capabilities"] = {"http": caps.http, "sse": caps.sse}
                sessions = {}
                for label in ("a", "b", "empty"):
                    session = await agent.new_session(
                        cwd=str(workspace), mcp_servers=[]
                    )
                    sessions[label] = session.session_id
                    # Every Agent exists before MCP discovery: test the upstream snapshot boundary.
                    agent._build_inbound([], agent._sessions[session.session_id])
                for label in ("a", "b"):
                    await agent.resume_session(
                        cwd=str(workspace),
                        session_id=sessions[label],
                        mcp_servers=[config(label)],
                    )
                for label in ("a", "b", "a"):
                    await schema(sessions[label], label)
                    await call(sessions[label], label)
                await schema(sessions["empty"], "empty")

                for name, ordinary in (
                    ("ordinary_before", ordinary_before),
                    ("ordinary_after", Agent(framework)),
                ):
                    visible = sorted(
                        key for key in ordinary.tools if key.startswith("mcp.shared_")
                    )
                    result["isolation_checks"].append(
                        {
                            "session": name,
                            "visible": visible,
                            "unexpected": visible,
                            "passed": not visible,
                        }
                    )
                    if TOOL in ordinary.tools:
                        result["ordinary_cross_session_call"] = json.loads(
                            await ordinary.tools[TOOL].run()
                        )
                result["global_registry_changed"] = REGISTRY != initial_registry
                result["distinct_runtime_agents"] = len(
                    {id(a) for a in agent._runtime_agents.values()}
                )

                await agent.resume_session(
                    cwd=str(workspace),
                    session_id=sessions["a"],
                    mcp_servers=[config("a2")],
                )
                await call(sessions["a"], "a2")
                records = [
                    json.loads(p.read_text())
                    for p in sorted(run_dir.glob("*.pid.json"))
                ]
                old = [r for r in records if r["label"] == "a"]
                result["replacement"] = {
                    "eligible": len(old),
                    "alive_pids": await observe_processes(old),
                }

                await agent.shutdown()
                result["shutdown"] = {
                    "eligible": len(records),
                    "connected_clients": sum(
                        client.is_connected() for client in clients
                    ),
                    "alive_pids": await observe_processes(records),
                }
                result["servers"] = records
        finally:
            # Measure first, then explicitly clean even deliberately ablated lifecycles.
            await asyncio.gather(*(original_stop(channel) for channel in channels))
            await asyncio.gather(*(client.close() for client in clients))
            REGISTRY.clear()
            REGISTRY.update(initial_registry)
            records = [json.loads(p.read_text()) for p in run_dir.glob("*.pid.json")]
            result["harness_cleanup_alive_pids"] = await observe_processes(
                records, grace=3
            )
    result["call_successes"] = sum(call["passed"] for call in result["calls"])
    result["isolation_passes"] = sum(
        check["passed"] for check in result["isolation_checks"]
    )
    result["versions"] = {
        name: importlib.metadata.version(name)
        for name in (
            "bub",
            "bub-acp-server",
            "bub-mcp",
            "fastmcp",
            "agent-client-protocol",
        )
    }
    result["bub_entry_points"] = sorted(
        entry.name for entry in importlib.metadata.entry_points(group="bub")
    )
    return result


async def run_matrix(args) -> None:
    jobs = [(variant, repeat) for repeat in range(args.repeats) for variant in VARIANTS]
    random.Random(args.seed).shuffle(jobs)
    semaphore = asyncio.Semaphore(args.jobs)

    async def run_one(variant, repeat):
        async with semaphore:
            with tempfile.TemporaryDirectory(prefix="acp-mcp-ablation-") as temp:
                run_dir = Path(temp)
                env = os.environ.copy()
                env.pop("PYTHONPATH", None)
                env["HOME"] = str(run_dir / "home")
                process = await asyncio.create_subprocess_exec(
                    args.python,
                    str(Path(__file__).resolve()),
                    "--case",
                    variant,
                    "--run-dir",
                    str(run_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=90
                    )
                except BaseException:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.communicate(), timeout=5)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        await asyncio.wait_for(process.communicate(), timeout=5)
                    raise
                if process.returncode:
                    raise RuntimeError(
                        f"{variant}/{repeat} failed: {stderr.decode()[-6000:]}"
                    )
                result = json.loads(stdout.decode().strip().splitlines()[-1])
                result["repeat"] = repeat + 1
                print(
                    f"{variant} {repeat + 1}: calls={result['call_successes']}/4 isolation={result['isolation_passes']}/6 shutdown_alive={len(result['shutdown']['alive_pids'])}",
                    flush=True,
                )
                if result["harness_cleanup_alive_pids"]:
                    raise RuntimeError(f"Probe cleanup failed: {result}")
                return result

    results = await asyncio.gather(*(run_one(*job) for job in jobs))
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "source_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], text=True)
        ),
        "lock_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[3] / "uv.lock").read_bytes()
        ).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": platform.platform(),
        "python": args.python,
        "repeats": args.repeats,
        "seed": args.seed,
        "jobs": args.jobs,
        "transport": "real stdio subprocesses",
        "model": "deterministic schema probe; no external LLM",
        "execution_order": jobs,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")


def isolated_python(directory: Path) -> str:
    """Install just Bub, ACP and MCP, constrained by the checked-in lockfile."""
    root = Path(__file__).resolve().parents[3]
    packages = tomllib.loads((root / "uv.lock").read_text())["package"]
    constraints = directory / "constraints.txt"
    constraints.write_text(
        "\n".join(
            f"{p['name']}=={p['version']}"
            for p in packages
            if "registry" in p["source"]
        )
    )
    source = next(p["source"]["git"] for p in packages if p["name"] == "bub")
    url, revision = source.split("#", 1)
    python = str(directory / "venv" / "bin" / "python")
    subprocess.run(
        ["uv", "venv", str(directory / "venv"), "--python", sys.executable], check=True
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            python,
            "--constraint",
            str(constraints),
            f"bub @ git+{url}@{revision}",
            str(root / "packages" / "bub-acp-server"),
            str(root / "packages" / "bub-mcp"),
        ],
        check=True,
    )
    return python


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=VARIANTS)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--isolated", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--output", type=Path, default=Path("mcp-ablation.json"))
    args = parser.parse_args()
    if args.case:
        if args.run_dir is None:
            parser.error("--case requires --run-dir")
        print(json.dumps(asyncio.run(probe(args.case, args.run_dir.resolve()))))
    else:
        if args.repeats < 1 or args.jobs < 1:
            parser.error("--repeats and --jobs must be positive")
        if args.isolated:
            with tempfile.TemporaryDirectory(prefix="acp-ablation-env-") as directory:
                args.python = isolated_python(Path(directory))
                asyncio.run(run_matrix(args))
        else:
            asyncio.run(run_matrix(args))


if __name__ == "__main__":
    main()
