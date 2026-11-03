"""Report `bub chat` status to herdr-core through OSC 7501 escape sequences.

herdr-core treats any terminal whose program prints OSC 7501 as an agent, the
same way Claude Code and Codex report natively:

    ESC ] 7501 ; state=working:app=bub:msg=<base64> ESC \\

Only the interactive `bub chat` REPL running inside a herdr-core terminal
(``HC_TERMINAL_ID`` is set) reports; gateway channels have no terminal of their own.
"""

from __future__ import annotations

import atexit
import base64
import os
import re
import sys

from bub import hookimpl
from bub.envelope import Envelope, field_of
from bub.hooks.interception import ToolCall
from bub.turn import TurnState

APP = "bub"
MSG_MAX = 2048
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def encode(text: str, limit: int = MSG_MAX) -> str:
    return base64.b64encode(_CONTROL.sub(" ", text)[:limit].encode()).decode()


def sequence(state: str, *, msg: str | None = None) -> str:
    fields = [f"state={state}", f"app={APP}"]
    if msg:
        fields.append(f"msg={encode(msg)}")
    return f"\033]7501;{':'.join(fields)}\033\\"


def _is_chat(argv: list[str]) -> bool:
    args = iter(argv[1:])
    for arg in args:
        if arg in ("-w", "--workspace"):
            next(args, None)
        elif not arg.startswith("-"):
            return arg == "chat"
    return False


class Reporter:
    def __init__(self, fd: int | None) -> None:
        self._fd = fd

    @classmethod
    def from_environment(cls) -> Reporter:
        if not os.environ.get("HC_TERMINAL_ID") or not _is_chat(sys.argv):
            return cls(None)
        try:
            fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
        except OSError:
            return cls(None)
        reporter = cls(fd)
        reporter.report("idle")
        atexit.register(reporter.write, "\033]7501;state=clear\033\\")
        return reporter

    def write(self, data: str) -> None:
        if self._fd is None:
            return
        try:
            os.write(self._fd, data.encode())
        except OSError:
            self._fd = None

    def report(self, state: str, *, msg: str | None = None) -> None:
        self.write(sequence(state, msg=msg))


_reporter = Reporter.from_environment()


def _is_cli(message: Envelope | None) -> bool:
    return message is not None and field_of(message, "channel") == "cli"


def _first_line(text: str) -> str | None:
    lines = text.strip().splitlines()
    return lines[0] if lines else None


@hookimpl
def load_state(message: Envelope, session_id: str) -> None:
    if _is_cli(message):
        _reporter.report("working")


@hookimpl
def before_tool_call(call: ToolCall, state: TurnState) -> None:
    _reporter.report("working", msg=call.tool)


@hookimpl
def save_state(
    session_id: str, state: TurnState, message: Envelope, model_output: str
) -> None:
    if _is_cli(message):
        _reporter.report("done", msg=_first_line(model_output))


@hookimpl
def on_error(stage: str, error: Exception, message: Envelope | None) -> None:
    if message is None or _is_cli(message):
        _reporter.report("error", msg=f"{stage}: {error}")
