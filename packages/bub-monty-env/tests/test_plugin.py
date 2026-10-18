from __future__ import annotations

from pathlib import Path

import bub
import pluggy
import pytest
from bub.builtin.codemode import CODE_TOOLS_STATE_KEY, run_code
from bub.environment import ENVIRONMENT_STATE_KEY, LocalEnvironment
from bub.errors import BubError
from bub.core.tools import ToolContext, tool
from bub.hooks import BUB_HOOK_NAMESPACE, BubHookSpecs, hookimpl
from bub.hooks.runtime import HookRuntime
from bub_monty_env import MontyEnvironment, MontyWrappedEnvironment
from bub_monty_env.config import MontyEnvConfig
from bub_monty_env.plugin import MontyEnvPlugin


@pytest.mark.parametrize(
    ("sessions", "session_id", "expected"),
    [
        (["qq:*"], "qq:group:g1", True),
        (["qq:*"], "cli:default", False),
        (["*"], "cli:default", True),
        ([], "qq:group:g1", False),
    ],
)
def test_session_patterns(sessions, session_id, expected) -> None:
    assert MontyEnvConfig(sessions=sessions).matches(session_id) is expected


def test_config_loads_from_env(monkeypatch) -> None:
    monkeypatch.setenv("BUB_MONTY_ENV_SESSIONS", '["qq:*"]')
    monkeypatch.setenv("BUB_MONTY_ENV_FILESYSTEM", "read-only")
    monkeypatch.setenv("BUB_MONTY_ENV_MAX_MEMORY_MB", "null")
    config = MontyEnvConfig()
    assert config.sessions == ["qq:*"]
    options = config.options()
    assert options.filesystem == "read-only"
    assert "max_memory" not in options.limits


def test_options_carry_limits() -> None:
    limits = MontyEnvConfig(max_memory_mb=64, max_suspensions=5).options().limits
    assert limits["max_memory"] == 64 * 1024 * 1024
    assert limits["max_suspensions"] == 5


class OtherEnvironment(LocalEnvironment):
    pass


class OtherPlugin:
    """A plugin providing its own environment, like bub-docker-env or bub-sandbox-env."""

    def __init__(self, sessions: str = "*") -> None:
        self.sessions = sessions

    @hookimpl
    async def provide_environment(self, session_id, workspace):
        if not session_id.startswith(self.sessions.rstrip("*")):
            return None
        return OtherEnvironment(workspace)


class FakeFramework:
    def __init__(self) -> None:
        self.plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
        self.plugin_manager.add_hookspecs(BubHookSpecs)


def make_runtime(*plugins) -> HookRuntime:
    framework = FakeFramework()
    # Registered first, like an entry point loaded before the others: tryfirst still wins.
    framework.plugin_manager.register(MontyEnvPlugin(framework), name="monty-env")
    for index, item in enumerate(plugins):
        framework.plugin_manager.register(item, name=f"other-{index}")
    return HookRuntime(framework.plugin_manager)


@pytest.fixture
def sessions(monkeypatch):
    def set_sessions(patterns):
        monkeypatch.setattr(
            bub, "ensure_config", lambda cls: MontyEnvConfig(sessions=patterns)
        )

    set_sessions(["*"])
    return set_sessions


async def provide(runtime: HookRuntime, session_id: str, workspace: Path):
    return await runtime.call_first(
        "provide_environment", session_id=session_id, workspace=workspace
    )


async def test_wraps_the_environment_other_plugins_provide(sessions, tmp_path) -> None:
    environment = await provide(make_runtime(OtherPlugin()), "qq:g1", tmp_path)
    try:
        assert isinstance(environment, MontyWrappedEnvironment)
        assert isinstance(environment.environment, OtherEnvironment)
        output: list[str] = []
        await environment.run_code(
            "print(1 + 1)", tools=[], call_tool=None, write=output.append
        )
        assert output == ["2\n"]
    finally:
        await environment.close()


async def test_wraps_a_host_environment_when_no_other_plugin_provides_one(
    sessions, tmp_path
) -> None:
    environment = await provide(make_runtime(OtherPlugin("qq:*")), "cli:x", tmp_path)
    try:
        assert isinstance(environment, MontyWrappedEnvironment)
        assert type(environment.environment) is LocalEnvironment
        assert environment.workspace == str(tmp_path)
    finally:
        await environment.close()


async def test_unmatched_sessions_keep_the_other_environment(
    sessions, tmp_path
) -> None:
    sessions(["qq:*"])
    environment = await provide(make_runtime(OtherPlugin()), "cli:x", tmp_path)
    assert type(environment) is OtherEnvironment


@tool(name="add")
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def test_bub_run_code_tool_runs_in_monty(tmp_path: Path) -> None:
    environment = MontyEnvironment(tmp_path)
    state = {CODE_TOOLS_STATE_KEY: [add], ENVIRONMENT_STATE_KEY: environment}
    context = ToolContext(tape=None, state=state)  # type: ignore[arg-type]
    try:
        output = await run_code.run(
            code="print(await tools.add(a=2, b=3))", context=context
        )
        assert output == "5\n"
        with pytest.raises(BubError) as info:
            await run_code.run(
                code="print('partial')\nraise ValueError('nope')", context=context
            )
        assert "ValueError: nope" in info.value.message
        assert info.value.details["output"] == "partial\n"
    finally:
        await environment.close()
