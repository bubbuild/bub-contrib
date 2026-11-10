from __future__ import annotations

import bub
import pytest
from bub_sandbox_env import environment, plugin
from bub_sandbox_env.config import SandboxEnvConfig
from bub_sandbox_env.environment import SandboxEnvironment, expand, scrub_env
from bub_sandbox_env.policy import (
    Policy,
    bwrap_command,
    seatbelt_command,
    seatbelt_profile,
)


@pytest.mark.parametrize(
    ("sessions", "session_id", "expected"),
    [
        (["qq:*"], "qq:group:g1", True),
        (["qq:*"], "cli:default", False),
        (["qq:*", "feishu:*"], "feishu:chat", True),
        (["*"], "cli:default", True),
        ([], "qq:group:g1", False),
    ],
)
def test_session_patterns(sessions, session_id, expected) -> None:
    assert SandboxEnvConfig(sessions=sessions).matches(session_id) is expected


def test_lists_load_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BUB_SANDBOX_ENV_SESSIONS", '["qq:*"]')
    monkeypatch.setenv("BUB_SANDBOX_ENV_WRITABLE", '["~/.cache/uv", "~/.npm"]')
    config = SandboxEnvConfig()
    assert config.sessions == ["qq:*"]
    assert config.writable == ["~/.cache/uv", "~/.npm"]


def test_defaults_are_locked_down() -> None:
    # Installing a sandbox plugin should never silently leave sessions on the host.
    config = SandboxEnvConfig()
    assert config.matches("telegram:12345")
    assert config.network is False
    assert "~/.ssh" in config.deny_read
    assert "$BUB_HOME" in config.deny_read


def test_auto_backend_follows_the_platform(monkeypatch) -> None:
    from bub_sandbox_env import config as config_module

    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    assert SandboxEnvConfig().resolved_backend() == "seatbelt"
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    assert SandboxEnvConfig().resolved_backend() == "bwrap"
    assert SandboxEnvConfig(backend="seatbelt").resolved_backend() == "seatbelt"


def test_expand_handles_home_and_bub_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / "bubhome"))
    assert expand("$BUB_HOME") == str((tmp_path / "bubhome").resolve())
    assert expand("$BUB_HOME/config.yml").endswith("bubhome/config.yml")
    assert not expand("~/.ssh").startswith("~")


def test_scrub_env_drops_secrets_case_insensitively() -> None:
    environ = {
        "PATH": "/bin",
        "OPENAI_API_KEY": "x",
        "github_token": "x",
        "BUB_MODEL": "x",
        "AWS_REGION": "x",
        "HOME": "/home/u",
    }
    patterns = SandboxEnvConfig().env_deny
    assert scrub_env(environ, patterns) == {"PATH": "/bin", "HOME": "/home/u"}


def test_seatbelt_passes_paths_as_parameters() -> None:
    policy = Policy(writable=('/ws/"quoted")',), deny_read=("/home/u/.ssh",))
    profile = seatbelt_profile(policy)
    assert "(deny default)" in profile
    assert "/ws" not in profile and ".ssh" not in profile
    # Writable roots come after the denials, so they win.
    assert profile.index('(param "DENY0")') < profile.index('(param "WRITE0")')
    assert "network" not in profile

    command = seatbelt_command(policy, ["bash", "-c", "ls"])
    assert command[0] == "/usr/bin/sandbox-exec"
    assert command[command.index('WRITE0=/ws/"quoted")') - 1] == "-D"
    assert command[command.index("DENY0=/home/u/.ssh") - 1] == "-D"
    assert command[-4:] == ["--", "bash", "-c", "ls"]


def test_the_more_specific_path_wins() -> None:
    policy = Policy(
        writable=("/home/u/.bub/ws", "/ws"),
        deny_read=("/home/u/.bub", "/ws/.env", "/ws"),
    )
    # A root equal to a denied path counts as outer, so the root stays usable.
    assert policy.outer_denied == ("/home/u/.bub", "/ws")
    assert policy.inner_denied == ("/ws/.env",)
    profile = seatbelt_profile(policy)
    assert profile.index("(deny file-read* (subpath") < profile.index("WRITE0")
    assert profile.index("WRITE1") < profile.index(
        '(deny file-read* file-write* (subpath (param "DENY2")))'
    )
    command = seatbelt_command(policy, ["true"])
    assert "DENY2=/ws/.env" in command

    bwrap = bwrap_command(policy, ["true"], tmpdir="/t", cwd="/")
    # Masks only apply to existing paths; none of these exist, so no masks at all.
    assert "--tmpfs" not in bwrap


def test_seatbelt_network_is_opt_in() -> None:
    assert "(allow network*)" in seatbelt_profile(Policy(writable=(), network=True))
    assert "(allow network*)" not in seatbelt_profile(Policy(writable=()))


def test_bwrap_command(tmp_path) -> None:
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret_file = tmp_path / "token"
    secret_file.write_text("x")
    policy = Policy(
        writable=(str(tmp_path / "ws"), str(tmp_path / "t")),
        deny_read=(str(secret_dir), str(secret_file), str(tmp_path / "missing")),
    )
    command = bwrap_command(
        policy, ["ls"], tmpdir=str(tmp_path / "t"), cwd=str(tmp_path / "ws")
    )
    joined = " ".join(command)
    assert "--ro-bind / /" in joined
    assert f"--bind {tmp_path / 't'} /tmp" in joined
    assert "--unshare-net" in command
    assert f"--tmpfs {secret_dir} --remount-ro {secret_dir}" in joined
    assert f"--ro-bind /dev/null {secret_file}" in joined
    assert "missing" not in joined
    assert f"--bind-try {tmp_path / 'ws'} {tmp_path / 'ws'}" in joined
    # Must stay in Bub's process group so stopping reaches the whole tree.
    assert "--new-session" not in command
    assert command[-4:] == ["--chdir", str(tmp_path / "ws"), "--", "ls"]

    open_net = Policy(writable=(), network=True)
    assert "--unshare-net" not in bwrap_command(open_net, ["ls"], tmpdir="/t", cwd="/")


def test_environment_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(environment, "darwin_user_dirs", lambda: ("/var/folders/T",))
    extra = tmp_path / "cache"
    env = SandboxEnvironment(
        SandboxEnvConfig(backend="seatbelt", writable=[str(extra)], network=True),
        workspace=tmp_path,
    )
    try:
        assert env.policy.writable == (
            str(tmp_path.resolve()),
            env.tmpdir,
            "/var/folders/T",
            str(extra.resolve()),
        )
        assert env.policy.network is True
        assert env.process_env({"A": "1"})["TMPDIR"] == env.tmpdir
        assert env.process_env({"A": "1"})["A"] == "1"
    finally:
        import asyncio

        asyncio.run(env.close())
    assert not (tmp_path / env.tmpdir).exists()


def test_missing_backend_fails_closed(tmp_path) -> None:
    env = SandboxEnvironment(
        SandboxEnvConfig(backend="bwrap", bwrap="no-such-bwrap"), workspace=tmp_path
    )
    with pytest.raises(environment.SandboxError, match="bubblewrap not found"):
        env.wrap(["true"], str(tmp_path))


def test_plugin_leaves_other_sessions_to_bub(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        bub, "ensure_config", lambda cls: SandboxEnvConfig(sessions=["qq:*"])
    )
    assert plugin.provide_environment("cli:default", tmp_path) is None
    env = plugin.provide_environment("qq:group:g1", tmp_path)
    assert isinstance(env, SandboxEnvironment)
    assert env.workspace == str(tmp_path.resolve())
    assert env.resolve_path("notes.md") == str(tmp_path.resolve() / "notes.md")
