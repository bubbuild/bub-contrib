from __future__ import annotations

import base64
import os

import pytest
from bub.hooks.interception import ToolCall

from bub_herdr import plugin


class Recorder(plugin.Reporter):
    def __init__(self) -> None:
        super().__init__(None)
        self.sent: list[str] = []

    def write(self, data: str) -> None:
        self.sent.append(data)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(plugin, "_reporter", rec)
    return rec


def _msg(seq: str) -> str:
    field = next(
        f for f in seq.removesuffix("\033\\").split(":") if f.startswith("msg=")
    )
    return base64.b64decode(field.removeprefix("msg=")).decode()


def test_sequence_format() -> None:
    assert plugin.sequence("working") == "\033]7501;state=working:app=bub\033\\"


def test_msg_is_base64_without_control_characters() -> None:
    seq = plugin.sequence("done", msg="line\x1b]evil\x07 你好")
    assert _msg(seq) == "line ]evil  你好"


def test_msg_is_truncated() -> None:
    assert len(_msg(plugin.sequence("done", msg="x" * 5000))) == plugin.MSG_MAX


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["bub", "chat"], True),
        (["bub", "-w", "/tmp", "chat"], True),
        (["bub", "run", "chat"], False),
        (["bub", "--workspace=/tmp", "chat", "--chat-id", "x"], True),
        (["bub", "gateway"], False),
        (["bub"], False),
    ],
)
def test_is_chat(argv: list[str], expected: bool) -> None:
    assert plugin._is_chat(argv) is expected


def test_disabled_outside_herdr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    monkeypatch.setattr(plugin.sys, "argv", ["bub", "chat"])
    reporter = plugin.Reporter.from_environment()
    assert reporter._fd is None


def test_turn_lifecycle_for_cli(recorder: Recorder) -> None:
    message = {"channel": "cli", "content": "hi"}
    plugin.load_state(message, "cli:local")
    plugin.before_tool_call(ToolCall(run_id="r", tool="bash", arguments={}), {})
    plugin.save_state("cli:local", {}, message, "1+1 等于 2。\nmore")
    states = [s.split(":")[0].removeprefix("\033]7501;") for s in recorder.sent]
    assert states == ["state=working", "state=working", "state=done"]
    assert _msg(recorder.sent[1]) == "bash"
    assert _msg(recorder.sent[2]) == "1+1 等于 2。"


def test_other_channels_are_ignored(recorder: Recorder) -> None:
    message = {"channel": "telegram", "content": "hi"}
    plugin.load_state(message, "telegram:1")
    plugin.save_state("telegram:1", {}, message, "ok")
    plugin.on_error("turn", RuntimeError("boom"), message)
    assert recorder.sent == []


def test_error_is_reported(recorder: Recorder) -> None:
    plugin.on_error("turn", RuntimeError("boom"), {"channel": "cli"})
    assert recorder.sent[0].startswith("\033]7501;state=error:app=bub:msg=")
    assert _msg(recorder.sent[0]) == "turn: boom"


def test_write_failure_disables_reporter() -> None:
    r, w = os.pipe()
    os.close(r)
    reporter = plugin.Reporter(w)
    reporter.report("idle")
    assert reporter._fd is None
