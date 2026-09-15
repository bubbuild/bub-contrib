import json
import subprocess
from types import SimpleNamespace

from bub.hooks.interception import ToolCall

from bub_hol_guard import plugin


def _call(tool: str = "bash", **arguments: object) -> ToolCall:
    return ToolCall(run_id="run", tool=tool, arguments=dict(arguments))


def _completed(payload: object, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")


def test_non_bash_skips_guard(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError

    monkeypatch.setattr(plugin.subprocess, "run", fail)
    assert plugin.before_tool_call(_call("read_file", path="x"), {}) is None


def test_explicit_allow_proceeds(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            {"classification": {"explicitly_benign": True}, "minimum_action": "allow"}
        ),
    )
    assert plugin.before_tool_call(_call(cmd="git status"), {}) is None


def test_review_denies(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            {"classification": {"explicitly_benign": False}, "minimum_action": "review"}
        ),
    )
    decision = plugin.before_tool_call(_call(cmd="rm -rf build"), {})
    assert decision is not None
    assert decision.action == "deny"
    assert decision.message == "HOL_GUARD_DENY"


def test_implicit_allow_denies(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            {"classification": {}, "minimum_action": "allow"}
        ),
    )
    decision = plugin.before_tool_call(_call(cmd="git status"), {})
    assert decision is not None
    assert decision.action == "deny"


def test_malformed_output_denies(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
    )
    decision = plugin.before_tool_call(_call(cmd="git status"), {})
    assert decision is not None
    assert decision.message == "HOL_GUARD_INVALID_OUTPUT"


def test_nonzero_exit_denies(monkeypatch) -> None:
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *args, **kwargs: _completed({}, returncode=2),
    )
    decision = plugin.before_tool_call(_call(cmd="git status"), {})
    assert decision is not None
    assert decision.message == "HOL_GUARD_ERROR"


def test_missing_guard_denies(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(plugin.subprocess, "run", missing)
    decision = plugin.before_tool_call(_call(cmd="git status"), {})
    assert decision is not None
    assert decision.message == "HOL_GUARD_UNAVAILABLE"


def test_timeout_denies(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="hol-guard", timeout=10)

    monkeypatch.setattr(plugin.subprocess, "run", timeout)
    decision = plugin.before_tool_call(_call(cmd="git status"), {})
    assert decision is not None
    assert decision.message == "HOL_GUARD_TIMEOUT"


def test_missing_command_denies(monkeypatch) -> None:
    decision = plugin.before_tool_call(_call(), {})
    assert decision is not None
    assert decision.message == "HOL_GUARD_INVALID_COMMAND"
