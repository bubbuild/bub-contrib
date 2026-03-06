import asyncio
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

# Add src to path for direct testing without install.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

bub_module = types.ModuleType("bub")
bub_module.hookimpl = lambda func: func
sys.modules.setdefault("bub", bub_module)

bub_types_module = types.ModuleType("bub.types")
bub_types_module.State = dict
sys.modules.setdefault("bub.types", bub_types_module)

from bub_kapy import plugin


class FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        assert stdin is not None
        assert b"Test prompt" in stdin
        return self._stdout, self._stderr


async def test_kapy_model() -> None:
    workspace = Path(os.getcwd()) / ".tmp-kapy-test"
    workspace.mkdir(exist_ok=True)
    state = {"_runtime_workspace": str(workspace)}

    created_commands: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        created_commands.append(list(args))
        return FakeProcess(
            b'{"thread_id":"kapy-thread-1"}\nKapybara reply',
            b"",
            0,
        )

    with patch.object(plugin.kapy_settings, "command", "kapybara chat --json -"), patch.object(
        plugin.kapy_settings, "resume_format", "resume {thread_id}"
    ), patch.object(plugin.kapy_settings, "copy_skills", False), patch(
        "bub_kapy.plugin.asyncio.create_subprocess_exec", new=fake_exec
    ):
        model = plugin.KapyModel(session_id="session-1", state=state)
        results = []
        async for chunk in model.run_model("Test prompt"):
            results.append(chunk)

    output = "".join(results)
    assert output == "Kapybara reply"
    assert created_commands == [["kapybara", "chat", "--json", "-"]]

    threads_file = workspace / plugin.THREADS_FILE
    assert json.loads(threads_file.read_text()) == {"session-1": "kapy-thread-1"}


if __name__ == "__main__":
    asyncio.run(test_kapy_model())
