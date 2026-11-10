"""Against the real sandbox backend of this host; skipped when it cannot run."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from bub_sandbox_env.config import SandboxEnvConfig
from bub_sandbox_env.environment import SandboxEnvironment


def _backend_works() -> bool:
    if sys.platform == "darwin":
        argv = ["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)", "true"]
    elif shutil.which("bwrap"):
        # Unprivileged user namespaces may be disabled (e.g. Ubuntu's AppArmor policy).
        argv = ["bwrap", "--ro-bind", "/", "/", "true"]
    else:
        return False
    try:
        return subprocess.run(argv, capture_output=True, check=False).returncode == 0
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _backend_works(), reason="no usable sandbox")


@pytest.fixture
def secret(tmp_path) -> Path:
    path = tmp_path / "secret" / "token"
    path.parent.mkdir()
    path.write_text("s3cret")
    return path


@pytest.fixture
def env(tmp_path, secret, monkeypatch):
    monkeypatch.setenv("FAKE_API_KEY", "leaked")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sandbox = SandboxEnvironment(
        SandboxEnvConfig(deny_read=[str(secret.parent)]), workspace=workspace
    )
    yield sandbox
    asyncio.run(sandbox.close())


async def _run(env: SandboxEnvironment, command: str) -> tuple[int, str, str]:
    process = await env.spawn(command)
    process.close_stdin()
    stdout, stderr = await asyncio.gather(process.stdout.read(), process.stderr.read())
    return await process.wait(), stdout.decode(), stderr.decode()


def test_workspace_and_tmp_are_writable(env) -> None:
    code, out, _ = asyncio.run(
        _run(env, 'echo hi > a.txt && cat a.txt && echo t > "$TMPDIR/t" && pwd')
    )
    assert code == 0
    assert out.split() == ["hi", env.workspace]


def test_writes_outside_are_refused(env) -> None:
    target = Path.home() / f".bub-sandbox-test-{os.getpid()}"
    code, _, _ = asyncio.run(_run(env, f"touch {target}"))
    assert code != 0
    assert not target.exists()
    with pytest.raises(PermissionError):
        asyncio.run(env.write_text(str(target), "x"))


def test_denied_paths_are_unreadable(env, secret) -> None:
    code, out, _ = asyncio.run(_run(env, f"cat {secret}"))
    assert code != 0 and "s3cret" not in out
    with pytest.raises(OSError):
        asyncio.run(env.read_text(str(secret)))


def test_secrets_are_scrubbed_and_network_is_off(env) -> None:
    code, out, _ = asyncio.run(_run(env, 'echo "${FAKE_API_KEY:-none}"'))
    assert out.strip() == "none"
    code, _, _ = asyncio.run(
        _run(
            env,
            "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\"",
        )
    )
    assert code != 0


def test_fs_round_trip(env) -> None:
    path = env.resolve_path("sub/dir/f.txt")
    asyncio.run(env.write_text(path, "héllo"))
    assert asyncio.run(env.read_text(path)) == "héllo"
    assert asyncio.run(env.read_bytes(path)) == "héllo".encode()


def test_run_code_runs_sandboxed(env) -> None:
    from bub.environment import CodeFailed

    output: list[str] = []

    async def call_tool(name, kwargs):
        raise AssertionError(name)

    asyncio.run(
        env.run_code("print(6 * 7)", tools=[], call_tool=call_tool, write=output.append)
    )
    assert "".join(output).strip() == "42"
    with pytest.raises(CodeFailed):
        asyncio.run(
            env.run_code(
                f"open({str(Path.home() / '.bub-sandbox-x')!r}, 'w')",
                tools=[],
                call_tool=call_tool,
                write=output.append,
            )
        )


def test_signal_stops_the_whole_tree(env) -> None:
    async def scenario() -> bool:
        process = await env.spawn("sleep 60 & sleep 60 & wait")
        process.close_stdin()
        await asyncio.sleep(0.5)
        process.signal(kill=True)
        await process.wait()
        deadline = time.monotonic() + 5
        while process.is_running() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        return process.is_running()

    assert asyncio.run(scenario()) is False
