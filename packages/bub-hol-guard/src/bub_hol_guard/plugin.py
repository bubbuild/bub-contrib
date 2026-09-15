import json
import subprocess
from typing import Any

from bub import hookimpl
from bub.hooks.interception import ToolCall
from bub.hooks.interception import ToolCallDecision
from bub.turn import TurnState

GUARD_TIMEOUT_SECONDS = 10


def _deny(code: str) -> ToolCallDecision:
    return ToolCallDecision.deny(code)


def _explicit_allow(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    classification = payload.get("classification")
    return (
        isinstance(classification, dict)
        and classification.get("explicitly_benign") is True
        and payload.get("minimum_action") == "allow"
    )


@hookimpl
def before_tool_call(call: ToolCall, state: TurnState) -> ToolCallDecision | None:
    del state
    if call.tool != "bash":
        return None

    command = call.arguments.get("cmd")
    if not isinstance(command, str) or not command.strip():
        return _deny("HOL_GUARD_INVALID_COMMAND")

    try:
        completed = subprocess.run(
            ["hol-guard", "command", "test", command, "--json"],
            capture_output=True,
            check=False,
            text=True,
            timeout=GUARD_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _deny("HOL_GUARD_UNAVAILABLE")
    except subprocess.TimeoutExpired:
        return _deny("HOL_GUARD_TIMEOUT")
    except OSError:
        return _deny("HOL_GUARD_ERROR")

    if completed.returncode != 0:
        return _deny("HOL_GUARD_ERROR")

    try:
        payload = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return _deny("HOL_GUARD_INVALID_OUTPUT")

    if _explicit_allow(payload):
        return None
    return _deny("HOL_GUARD_DENY")
