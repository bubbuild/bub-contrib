from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from bub.builtin.environment import LocalEnvironment
from bub.environment import CodeFailed, Environment
from bub_monty_env import (
    MontyCodeMixin,
    MontyEnvironment,
    MontyOptions,
    create_monty_environment,
)
from bub_monty_env.runner import UNSUPPORTED_HINT


async def no_tools(name, kwargs):
    raise AssertionError(f"unexpected tool call {name}")


async def run(env: Environment, code: str, call_tool=no_tools, tools=()) -> str:
    output: list[str] = []
    await env.run_code(
        code, tools=list(tools), call_tool=call_tool, write=output.append
    )
    return "".join(output)


@pytest.fixture
async def env(tmp_path):
    environment = MontyEnvironment(tmp_path)
    yield environment
    await environment.close()


async def test_prints_and_top_level_await(env) -> None:
    code = "import asyncio\nawait asyncio.sleep(0)\nprint(6 * 7)"
    assert await run(env, code) == "42\n"


async def test_tool_calls_reach_call_tool(env) -> None:
    calls = []

    async def call_tool(name, kwargs):
        calls.append((name, kwargs))
        return {"sum": kwargs["a"] + kwargs["b"]}

    code = "r = await tools.add(a=1, b=2)\nprint(r['sum'])"
    assert await run(env, code, call_tool, tools=["add"]) == "3\n"
    assert calls == [("add", {"a": 1, "b": 2})]


async def test_gathered_tool_calls_run_concurrently(env) -> None:
    async def call_tool(name, kwargs):
        await asyncio.sleep(0.3)
        return kwargs["i"]

    code = "import asyncio\nprint(await asyncio.gather(*[tools.wait(i=i) for i in range(4)]))"
    started = time.perf_counter()
    assert await run(env, code, call_tool, tools=["wait"]) == "[0, 1, 2, 3]\n"
    assert time.perf_counter() - started < 1.0


async def test_tool_errors_are_catchable(env) -> None:
    async def call_tool(name, kwargs):
        raise RuntimeError("denied by hook")

    code = "try:\n    await tools.x()\nexcept Exception as e:\n    print('caught', e)"
    assert await run(env, code, call_tool, tools=["x"]) == "caught denied by hook\n"


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("await tools.nope()", "unknown tool: tools.nope"),
        ("await tools.x(1)", "keyword arguments only"),
    ],
)
async def test_bad_tool_calls_fail(env, code, message) -> None:
    with pytest.raises(CodeFailed) as info:
        await run(env, code, tools=["x"])
    assert message in info.value.error


async def test_tool_results_cross_as_plain_data(env) -> None:
    from pydantic import BaseModel

    class Result(BaseModel):
        path: Path

    async def call_tool(name, kwargs):
        return Result(path=Path("/a/b"))

    assert (
        await run(env, "print((await tools.x())['path'])", call_tool, tools=["x"])
        == "/a/b\n"
    )


async def test_code_errors_raise_code_failed_with_output(env) -> None:
    output: list[str] = []
    with pytest.raises(CodeFailed) as info:
        await env.run_code(
            "print('before')\n1/0", tools=[], call_tool=no_tools, write=output.append
        )
    assert output == ["before\n"]
    assert info.value.error == "ZeroDivisionError: division by zero"
    assert "1/0" in info.value.traceback


async def test_unsupported_syntax_explains_monty(env) -> None:
    with pytest.raises(CodeFailed) as info:
        await run(env, "class A(B):\n    pass")
    assert UNSUPPORTED_HINT in info.value.error


async def test_cancel_stops_the_code(env) -> None:
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await run(env, "while True:\n    pass")
    # The pool still serves the next run.
    assert await run(env, "print('ok')") == "ok\n"


async def test_files_go_through_the_environment(env, tmp_path) -> None:
    (tmp_path / "in.txt").write_text("hello", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "text = Path('in.txt').read_text()\n"
        "Path('out/result.txt').write_text(text.upper())"
    )
    await run(env, code)
    assert (tmp_path / "out" / "result.txt").read_text(encoding="utf-8") == "HELLO"


async def test_read_only_filesystem(tmp_path) -> None:
    (tmp_path / "in.txt").write_text("hello", encoding="utf-8")
    env = MontyEnvironment(tmp_path, options=MontyOptions(filesystem="read-only"))
    try:
        assert (
            await run(
                env, "from pathlib import Path\nprint(Path('in.txt').read_text())"
            )
            == "hello\n"
        )
        with pytest.raises(CodeFailed, match="PermissionError"):
            await run(env, "from pathlib import Path\nPath('x.txt').write_text('x')")
    finally:
        await env.close()
    assert not (tmp_path / "x.txt").exists()


async def test_no_filesystem(tmp_path) -> None:
    (tmp_path / "in.txt").write_text("hello", encoding="utf-8")
    env = MontyEnvironment(tmp_path, options=MontyOptions(filesystem="none"))
    try:
        with pytest.raises(CodeFailed):
            await run(env, "from pathlib import Path\nPath('in.txt').read_text()")
    finally:
        await env.close()


async def test_host_environment_variables_are_hidden(env, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    code = "import os\nprint(os.getenv('OPENAI_API_KEY', 'none'), len(os.environ))"
    assert await run(env, code) == "none 0\n"


class RecordingEnvironment(LocalEnvironment):
    def __init__(self, workspace) -> None:
        super().__init__(workspace)
        self.reads: list[str] = []
        self.closed = False

    async def read_text(self, path: str) -> str:
        self.reads.append(path)
        return await super().read_text(path)

    def backend_name(self) -> str:
        return "recording"

    async def close(self) -> None:
        self.closed = True


async def test_wrapper_delegates_everything_but_run_code(tmp_path) -> None:
    (tmp_path / "in.txt").write_text("hi", encoding="utf-8")
    inner = RecordingEnvironment(tmp_path)
    env = create_monty_environment(inner)
    try:
        assert env.workspace == str(tmp_path)
        assert env.resolve_path("in.txt") == str(tmp_path / "in.txt")
        assert env.backend_name() == "recording"  # type: ignore[attr-defined]

        process = await env.spawn("echo spawned")
        process.close_stdin()
        assert (await process.stdout.read()).strip() == b"spawned"
        await process.wait()

        assert (
            await run(
                env, "from pathlib import Path\nprint(Path('in.txt').read_text())"
            )
            == "hi\n"
        )
        assert inner.reads == [str(tmp_path / "in.txt")]
    finally:
        await env.close()
    assert inner.closed


async def test_mixin_replaces_run_code_of_any_environment(tmp_path) -> None:
    class Mixed(MontyCodeMixin, RecordingEnvironment):
        monty_options = MontyOptions(filesystem="read-only")

    env = Mixed(tmp_path)
    try:
        # Monty has no third-party packages; a CPython subprocess would import this fine.
        with pytest.raises(CodeFailed, match="ModuleNotFoundError"):
            await run(env, "import pytest")
        assert await run(env, "print(1 + 1)") == "2\n"
    finally:
        await env.close()
    assert env.closed
