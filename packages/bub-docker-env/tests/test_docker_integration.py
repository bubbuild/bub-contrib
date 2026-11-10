"""Against a real Docker daemon; skipped when none is reachable."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

import pytest
from bub_docker_env import environment
from bub_docker_env.config import DockerEnvConfig
from bub_docker_env.environment import DockerEnvironment, _ContainerPool


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon not reachable"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "pool", _ContainerPool())
    docker_env = DockerEnvironment(
        DockerEnvConfig(), session_id="qq:test:integration", host_workspace=tmp_path
    )
    yield docker_env
    asyncio.run(docker_env.close())


async def _run(env: DockerEnvironment, command: str) -> tuple[int, str, str]:
    process = await env.spawn(command)
    process.close_stdin()
    stdout, stderr = await asyncio.gather(process.stdout.read(), process.stderr.read())
    return await process.wait(), stdout.decode(), stderr.decode()


def test_commands_run_in_the_container_as_the_host_user(env, tmp_path) -> None:
    code, out, _ = asyncio.run(
        _run(env, "pwd; id -u; test -e /.dockerenv && echo in-docker")
    )
    assert code == 0
    pwd, uid, marker = out.split()
    assert pwd == str(tmp_path.resolve())
    assert uid == str(os.getuid())
    assert marker == "in-docker"


def test_workspace_files_are_shared_with_the_host(env, tmp_path) -> None:
    # Same path inside and out: a host path Bub shows the model works as is.
    workspace = tmp_path.resolve()
    asyncio.run(env.write_text(f"{workspace}/sub/note.txt", "héllo"))
    assert (tmp_path / "sub" / "note.txt").read_text(encoding="utf-8") == "héllo"
    (tmp_path / "from-host.txt").write_text("hi", encoding="utf-8")
    assert asyncio.run(env.read_text(env.resolve_path("from-host.txt"))) == "hi"
    with pytest.raises(FileNotFoundError):
        asyncio.run(env.read_text(f"{workspace}/missing.txt"))


def test_only_the_workspace_and_tmp_are_writable(env, tmp_path) -> None:
    script = (
        "touch /usr/bub-probe 2>&1; touch /etc/bub-probe 2>&1;"
        " touch /tmp/ok && echo tmp-ok; touch ./ws-ok && echo ws-ok"
    )
    _, out, _ = asyncio.run(_run(env, script))
    assert out.count("Read-only file system") == 2
    assert "tmp-ok" in out and "ws-ok" in out
    assert (tmp_path / "ws-ok").exists()


def test_host_files_outside_the_workspace_are_not_visible(env, tmp_path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("s3cr3t", encoding="utf-8")
    _, out, _ = asyncio.run(
        _run(env, f"test -e {secret} && echo visible || echo hidden")
    )
    assert out.strip() == "hidden"


def test_no_network_by_default(env) -> None:
    script = "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\""
    code, _, err = asyncio.run(_run(env, script))
    assert code != 0
    assert "unreachable" in err.lower() or "error" in err.lower()


def test_kill_stops_the_whole_process_group(env) -> None:
    async def scenario() -> None:
        process = await env.spawn("sleep 300 & sleep 300 & wait")
        process.close_stdin()
        await asyncio.sleep(1.0)
        assert process.is_running()
        process.signal(kill=True)
        await asyncio.wait_for(process.wait(), timeout=15)
        deadline = time.monotonic() + 10
        while process.is_running() and time.monotonic() < deadline:
            await asyncio.sleep(0.3)
        assert not process.is_running()
        # The slim image has no pgrep; count leftover sleeps through /proc,
        # matching whole command lines so this script does not count itself.
        listing = (
            'n=0; for p in /proc/[0-9]*; do case "$(tr "\\0" " " < $p/cmdline 2>/dev/null)"'
            ' in "sleep 300 ") n=$((n+1));; esac; done; echo $n'
        )
        _, out, _ = await _run(env, listing)
        assert out.strip() == "0"

    asyncio.run(scenario())


def test_run_code_calls_back_into_bub(env) -> None:
    calls: list[tuple[str, dict]] = []
    output: list[str] = []

    async def call_tool(name: str, arguments: dict) -> object:
        calls.append((name, arguments))
        return {"echo": arguments["text"]}

    code = "result = await tools.echo(text='hi')\nprint(result['echo'], 1 + 1)"
    asyncio.run(
        env.run_code(code, tools=["echo"], call_tool=call_tool, write=output.append)
    )
    assert calls == [("echo", {"text": "hi"})]
    assert "".join(output).strip() == "hi 2"


def test_a_mount_that_is_not_the_workspace_is_refused(tmp_path, monkeypatch) -> None:
    """What a Bub-in-a-container deployment would hit without host_workspace."""

    monkeypatch.setattr(environment, "pool", _ContainerPool())
    workspace = tmp_path / "bub-side-workspace"
    workspace.mkdir()
    (workspace / "notes.md").write_text("x", encoding="utf-8")
    elsewhere = tmp_path / "unrelated-dir-on-docker-host"
    elsewhere.mkdir()
    (elsewhere / "other.txt").write_text("y", encoding="utf-8")
    config = DockerEnvConfig(host_workspace=str(elsewhere))
    env = DockerEnvironment(
        config, session_id="qq:test:wrong-mount", host_workspace=workspace
    )
    try:
        with pytest.raises(environment.DockerError, match="host_workspace"):
            asyncio.run(env.spawn("true"))
        listed = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"name={env.container.name}"],
            capture_output=True,
            check=False,
        )
        assert listed.stdout.strip() == b""
    finally:
        asyncio.run(env.close())
