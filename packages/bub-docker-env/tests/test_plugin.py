from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bub
import pytest
from bub_docker_env import environment, plugin
from bub_docker_env.config import DockerEnvConfig
from bub_docker_env.environment import DockerEnvironment, _ContainerPool
from loguru import logger


def _config(**overrides: object) -> DockerEnvConfig:
    return DockerEnvConfig.model_construct(
        **{**DockerEnvConfig().model_dump(), **overrides}
    )


@pytest.fixture
def logs():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    yield lines
    logger.remove(sink)


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch):
    monkeypatch.setattr(environment, "pool", _ContainerPool())


@pytest.mark.parametrize(
    ("sessions", "session_id", "expected"),
    [
        ("qq:*", "qq:group:g1", True),
        ("qq:*", "cli:default", False),
        ("qq:*,feishu:*", "feishu:chat", True),
        ("*", "cli:default", True),
        ("", "qq:group:g1", False),
    ],
)
def test_session_patterns(sessions, session_id, expected) -> None:
    assert _config(sessions=sessions).matches(session_id) is expected


def test_every_session_runs_in_docker_by_default() -> None:
    # Installing a sandbox plugin should never silently leave sessions on the host.
    assert DockerEnvConfig().sessions == "*"
    assert DockerEnvConfig().matches("telegram:12345")


def test_plugin_leaves_other_sessions_to_bub(monkeypatch, tmp_path, logs) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: _config(sessions="qq:*"))
    assert plugin.provide_environment("cli:default", tmp_path) is None
    env = plugin.provide_environment("qq:group:g1", tmp_path)
    assert isinstance(env, DockerEnvironment)
    # The workspace keeps its host path, so paths in Bub's prompt work in the container.
    workspace = str(tmp_path.resolve())
    assert env.workspace == workspace
    assert env.resolve_path("notes.md") == f"{workspace}/notes.md"
    assert any(
        "docker_env.session" in line
        and "qq:group:g1" in line
        and env.container.name in line
        for line in logs
    )


def test_skill_directories_are_mounted_read_only_at_host_paths(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    user_skills = home / ".agents" / "skills"
    user_skills.mkdir(parents=True)
    plugin_skills = tmp_path / "site-packages" / "skills"
    plugin_skills.mkdir(parents=True)
    workspace = tmp_path / "ws"
    (workspace / ".agents" / "skills").mkdir(parents=True)

    fake_skills = SimpleNamespace(
        __path__=[str(plugin_skills), str(workspace / ".agents" / "skills")]
    )
    monkeypatch.setattr(environment.Path, "home", lambda: home)
    monkeypatch.setattr(
        environment.importlib, "import_module", lambda name: fake_skills
    )

    mounts = environment.skill_mounts(workspace.resolve())
    assert mounts == [user_skills.resolve(), plugin_skills.resolve()]

    env = DockerEnvironment(
        _config(), session_id="qq:group:g1", host_workspace=workspace
    )
    joined = " ".join(env.container.run_args())
    for root in (user_skills.resolve(), plugin_skills.resolve()):
        assert f"--mount type=bind,source={root},target={root},readonly" in joined

    off = DockerEnvironment(
        _config(mount_skills=False), session_id="qq:group:g2", host_workspace=workspace
    )
    assert off.container.run_args().count("--mount") == 1
    # host_workspace means the daemon is elsewhere: skill paths would not map.
    remote = DockerEnvironment(
        _config(host_workspace="/srv/ws"),
        session_id="qq:group:g3",
        host_workspace=workspace,
    )
    assert remote.container.run_args().count("--mount") == 1


def test_container_is_locked_down(tmp_path) -> None:
    env = DockerEnvironment(
        _config(user="1000:1000", mount_skills=False),
        session_id="qq:group:g1",
        host_workspace=tmp_path,
    )
    args = env.container.run_args()
    joined = " ".join(args)
    for expected in (
        "--network none",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--pids-limit 256",
        "--memory 1g",
        "--user 1000:1000",
        f"--mount type=bind,source={tmp_path.resolve()},target={tmp_path.resolve()}",
        "--read-only --tmpfs /tmp:rw,exec,nosuid,nodev,size=512m",
    ):
        assert expected in joined
    # Only the workspace is mounted (skill mounts are covered separately).
    assert args.count("--mount") == 1
    assert "--volume" not in args
    assert args[-3:] == ["python:3.12-slim", "sleep", "infinity"]


def test_session_scope_gives_each_session_its_own_container(tmp_path) -> None:
    config = _config(scope="session")
    a = DockerEnvironment(config, session_id="qq:group:a", host_workspace=tmp_path)
    b = DockerEnvironment(config, session_id="qq:group:b", host_workspace=tmp_path)
    assert a.container is not b.container
    assert a.container.name != b.container.name


def test_shared_scope_removes_the_container_after_the_last_session(
    tmp_path, monkeypatch
) -> None:
    removed: list[str] = []

    async def fake_remove(self) -> None:
        removed.append(self.name)

    monkeypatch.setattr(environment.Container, "remove", fake_remove)
    config = _config(scope="shared")
    a = DockerEnvironment(config, session_id="qq:group:a", host_workspace=tmp_path)
    b = DockerEnvironment(config, session_id="qq:c2c:u", host_workspace=tmp_path)
    assert a.container is b.container

    asyncio.run(a.close())
    assert removed == []
    asyncio.run(b.close())
    assert removed == [a.container.name]


def test_spawn_runs_in_its_own_process_group(tmp_path, monkeypatch) -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_started(self) -> None:
        return None

    async def fake_exec(*args, **kwargs):
        seen.append(args)
        return asyncio.subprocess.Process.__new__(asyncio.subprocess.Process)

    monkeypatch.setattr(environment.Container, "ensure_started", fake_started)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    env = DockerEnvironment(
        _config(), session_id="qq:group:g1", host_workspace=tmp_path
    )

    process = asyncio.run(
        env.spawn("echo hi | wc -c", cwd="/workspace/sub", env={"A": "1"})
    )

    args = list(seen[0])
    name = env.container.name
    assert args[:2] == ["docker", "exec"]
    assert args[args.index("--workdir") + 1] == "/workspace/sub"
    assert args[args.index("--env") + 1] == "A=1"
    tail = args[args.index(name) + 1 :]
    assert tail[:4] == ["setsid", "--wait", "sh", "-c"]
    assert tail[5] == process.pgid_file
    assert tail[6:] == ["bash", "-c", "echo hi | wc -c"]
    # Stopping targets the recorded process group, not just docker exec.
    command = process._group_command("TERM")
    assert command[:3] == ["docker", "exec", name]
    assert command[-1] == process.pgid_file
    assert 'kill -TERM -"$(cat' in command[-2]


def test_missing_docker_cli_is_reported(tmp_path) -> None:
    env = DockerEnvironment(
        _config(docker="/nonexistent/docker"),
        session_id="qq:group:g1",
        host_workspace=tmp_path,
    )
    with pytest.raises(environment.DockerError, match="docker CLI not found"):
        asyncio.run(env.read_text("/workspace/x"))


def test_config_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("BUB_DOCKER_ENV_NETWORK", "bridge")
    monkeypatch.setenv("BUB_DOCKER_ENV_SCOPE", "shared")
    config = DockerEnvConfig()
    assert config.network == "bridge"
    assert config.scope == "shared"


@pytest.mark.parametrize(
    ("source", "options", "expected"),
    [
        ("/a/ws", "", ["--mount", "type=bind,source=/a/ws,target=/workspace"]),
        ("/a/b,c", "", ["--mount", 'type=bind,"source=/a/b,c",target=/workspace']),
        ("/a/ws", "z", ["--volume", "/a/ws:/workspace:z"]),
    ],
)
def test_bind_args(source, options, expected) -> None:
    assert environment.bind_args(source, "/workspace", options=options) == expected


def test_readonly_bind_args() -> None:
    assert environment.bind_args("/s", "/s", readonly=True) == [
        "--mount",
        "type=bind,source=/s,target=/s,readonly",
    ]
    assert environment.bind_args("/s", "/s", readonly=True, options="z") == [
        "--volume",
        "/s:/s:ro,z",
    ]


def test_mount_options_reject_paths_with_colons() -> None:
    with pytest.raises(environment.DockerError, match="contains ':'"):
        environment.bind_args("/a:b", "/workspace", options="z")


def test_host_workspace_user_none_and_extra_args(tmp_path) -> None:
    env = DockerEnvironment(
        _config(
            host_workspace="/srv/bub/ws",
            user="none",
            extra_run_args="--runtime=runsc --label 'a b=c'",
        ),
        session_id="qq:group:g1",
        host_workspace=tmp_path,
    )
    args = env.container.run_args()
    # The daemon-side source differs; the container path stays Bub's path.
    assert f"type=bind,source=/srv/bub/ws,target={tmp_path.resolve()}" in args
    assert "--user" not in args
    assert args[-6:-3] == ["--runtime=runsc", "--label", "a b=c"]


def _fake_docker(monkeypatch, ls_output: str) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    async def fake(config, *args, stdin=None):
        calls.append(args)
        if args[:1] == ("exec",):
            return 0, ls_output.encode(), b""
        return 0, b"", b""

    monkeypatch.setattr(environment, "_docker", fake)
    return calls


def test_mount_check_accepts_the_real_workspace(tmp_path, monkeypatch) -> None:
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    _fake_docker(monkeypatch, "notes.md\noutbox\n")
    env = DockerEnvironment(
        _config(), session_id="qq:group:g1", host_workspace=tmp_path
    )
    asyncio.run(env.container.ensure_started())
    assert env.container._started


def test_mount_check_refuses_a_wrong_mount_and_removes_the_container(
    tmp_path, monkeypatch, logs
) -> None:
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    calls = _fake_docker(monkeypatch, "etc\nunrelated\n")
    env = DockerEnvironment(
        _config(), session_id="qq:group:g1", host_workspace=tmp_path
    )
    with pytest.raises(environment.DockerError, match="host_workspace"):
        asyncio.run(env.container.ensure_started())
    assert not env.container._started
    assert calls[-1] == ("rm", "--force", env.container.name)


def test_mount_check_skips_an_empty_workspace(tmp_path, monkeypatch, logs) -> None:
    _fake_docker(monkeypatch, "")
    env = DockerEnvironment(
        _config(), session_id="qq:group:g1", host_workspace=tmp_path
    )
    asyncio.run(env.container.ensure_started())
    assert any("docker_env.mount.unverified" in line for line in logs)


def test_workdir_and_read_only_can_be_overridden(tmp_path) -> None:
    env = DockerEnvironment(
        _config(workdir="/workspace", read_only=False, mount_skills=False),
        session_id="qq:group:g1",
        host_workspace=tmp_path,
    )
    args = env.container.run_args()
    assert env.workspace == "/workspace"
    assert f"type=bind,source={tmp_path.resolve()},target=/workspace" in args
    assert "--read-only" not in args
