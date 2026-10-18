from __future__ import annotations

from pathlib import Path

import bub
import pytest
from bub.builtin.codemode import CODE_TOOLS_STATE_KEY, run_code
from bub.environment import ENVIRONMENT_STATE_KEY
from bub.errors import BubError
from bub.tools import ToolContext, tool
from bub_monty_env import MontyEnvironment, plugin
from bub_monty_env.config import MontyEnvConfig


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


def test_provide_environment_follows_sessions(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        bub, "ensure_config", lambda cls: MontyEnvConfig(sessions=["qq:*"])
    )
    assert plugin.provide_environment("cli:default", tmp_path) is None
    environment = plugin.provide_environment("qq:group:g1", tmp_path)
    assert isinstance(environment, MontyEnvironment)
    assert environment.workspace == str(tmp_path)


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
