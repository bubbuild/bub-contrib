from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from any_llm.types.completion import ChatCompletion

from bub.builtin.agent import Agent
from bub.framework import BubFramework
from bub.store import FileTapeStore
from bub.tools import REGISTRY, Tool
from bub_mcp.plugin import MCPChannel, MCPServerState
from bub_mcp.tools import MCPTool


class Provider:
    SUPPORTS_COMPLETION_STREAMING = False

    def __init__(self) -> None:
        self.replies: list[str | tuple[str, dict[str, Any]]] = []
        self.requests: list[dict[str, Any]] = []

    async def acompletion(self, **kwargs: Any) -> ChatCompletion:
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        message: dict[str, Any] = {"role": "assistant"}
        if isinstance(reply, str):
            message["content"] = reply
        else:
            name, arguments = reply
            message["tool_calls"] = [
                {
                    "id": f"call-{len(self.requests)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        return ChatCompletion.model_validate(
            {
                "id": "reply",
                "model": "test-model",
                "created": 0,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop"
                        if isinstance(reply, str)
                        else "tool_calls",
                        "message": message,
                    }
                ],
            }
        )


def definitions(request: dict[str, Any]) -> dict[str, Any]:
    return {
        item["function"]["name"]: item["function"]
        for item in request.get("tools") or []
    }


def system_prompt(request: dict[str, Any]) -> str:
    return "\n".join(
        message["content"]
        for message in request["messages"]
        if message["role"] == "system"
    )


type Runtime = tuple[BubFramework, Provider, Tool, list[str]]


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.workspace = tmp_path
    framework.load_builtin_hooks()
    provider = Provider()
    monkeypatch.setattr(
        "bub.builtin.model_runner.AnyLLM.create", lambda *args, **kwargs: provider
    )
    calls: list[str] = []

    def lookup(path: str) -> str:
        calls.append(path)
        return f"found {path}"

    remote = MCPTool.from_callable(
        lookup,
        name="mcp.notes_lookup",
        description="Read a remote note. Detailed instructions and parameter documentation stay in the native schema.",
    )
    return framework, provider, remote, calls


async def run(
    agent: Agent, session: str, *, allowed_tools: list[str] | None = None
) -> str:
    stream = await agent.run_stream(
        session_id=session,
        prompt="Read the note.",
        model="openrouter:test-model",
        allowed_tools=allowed_tools,
    )
    return "".join(
        [event.data.get("text", "") async for event in stream if event.kind == "final"]
    )


@pytest.mark.asyncio
async def test_getting_one_definition_keeps_builtins_direct_and_calls_the_original_handler(
    runtime: Runtime,
) -> None:
    framework, provider, remote, calls = runtime
    other = MCPTool.from_callable(
        lambda: "other", name="mcp.notes_other", description="Other work."
    )
    agent = make_agent(
        framework, tools=[*REGISTRY.values(), remote, other], skill_dirs=[]
    )
    provider.replies = [
        ("mcp_describe", {"names": ["mcp_notes_lookup"]}),
        ("mcp_notes_lookup", {"path": "note"}),
        "found note",
    ]
    assert await run(agent, "notes") == "found note"
    assert calls == ["note"]
    initial, loaded, finished = provider.requests
    builtins = {
        "bash",
        "bash_output",
        "bash_kill",
        "fs_read",
        "fs_write",
        "fs_edit",
        "skill",
        "tape_info",
        "tape_search",
        "tape_reset",
        "tape_handoff",
        "tape_anchors",
        "web_fetch",
        "subagent",
    }
    assert all(builtins <= definitions(request).keys() for request in provider.requests)
    assert "mcp_notes_lookup" not in definitions(initial)
    assert "mcp_notes_lookup: Read a remote note" in system_prompt(initial)
    assert "Detailed instructions" not in system_prompt(initial)
    assert "bash(" not in system_prompt(initial)
    assert definitions(loaded)["mcp_notes_lookup"] == remote.to_schema()["function"] | {
        "name": "mcp_notes_lookup"
    }
    assert "mcp_notes_lookup" not in system_prompt(loaded)
    assert "mcp_notes_other" in system_prompt(loaded)
    assert "mcp_notes_other" not in definitions(loaded)
    assert any(
        message.get("content") == "found note"
        for message in finished["messages"]
        if message["role"] == "tool"
    )
    assert not any(
        "Detailed instructions" in str(message)
        for message in loaded["messages"]
        if message["role"] == "tool"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_context", ["restart", "new_session", "reset", "handoff", "restricted"]
)
async def test_definition_reuse_follows_the_persisted_context_and_current_scope(
    runtime: Runtime, tmp_path: Path, next_context: str
) -> None:
    framework, provider, remote, calls = runtime
    directory = tmp_path / "store"
    agent = make_agent(
        framework, tools=[remote], tape_store=FileTapeStore(directory), skill_dirs=[]
    )
    provider.replies = [("mcp_describe", {"names": ["mcp_notes_lookup"]}), "ready"]
    assert await run(agent, "notes") == "ready"
    assert calls == []
    if next_context == "reset":
        await agent.tape.session_tape("notes", tmp_path).reset()
    elif next_context == "handoff":
        await agent.tape.session_tape("notes", tmp_path).handoff(name="next")
    restored = make_agent(
        framework, tools=[remote], tape_store=FileTapeStore(directory), skill_dirs=[]
    )
    provider.requests.clear()
    provider.replies = (
        [("mcp_notes_lookup", {"path": "note"}), "found note"]
        if next_context == "restart"
        else ["ready"]
    )
    session = "other" if next_context == "new_session" else "notes"
    allowed = [] if next_context == "restricted" else None
    assert await run(restored, session, allowed_tools=allowed) == (
        "found note" if next_context == "restart" else "ready"
    )
    exposed = definitions(provider.requests[0])
    assert ("mcp_notes_lookup" in exposed) == (next_context == "restart")
    assert ("mcp_notes_lookup" in system_prompt(provider.requests[0])) == (
        next_context not in {"restart", "restricted"}
    )
    assert calls == (["note"] if next_context == "restart" else [])


@pytest.mark.asyncio
async def test_definition_lookup_cannot_restore_a_tool_excluded_by_the_current_scope(
    runtime: Runtime,
) -> None:
    framework, provider, remote, calls = runtime
    other = MCPTool.from_callable(
        lambda: "other", name="mcp.notes_other", description="Other work."
    )
    agent = make_agent(framework, tools=[remote, other], skill_dirs=[])
    provider.replies = [("mcp_describe", {"names": ["mcp.notes_lookup"]}), "ready"]
    assert await run(agent, "notes", allowed_tools=["mcp_notes_other"]) == "ready"
    assert calls == []
    assert all(
        "mcp_notes_lookup" not in definitions(request) for request in provider.requests
    )
    assert all(
        "mcp_notes_lookup" not in system_prompt(request)
        for request in provider.requests
    )
    assert any(
        "unknown tool name" in message.get("content", "")
        for message in provider.requests[-1]["messages"]
        if message["role"] == "tool"
    )


def make_agent(framework: BubFramework, *, tools: list[Tool], **kwargs: Any) -> Agent:
    remote = [tool for tool in tools if isinstance(tool, MCPTool)]
    agent = Agent(
        framework,
        tools=[tool for tool in tools if not isinstance(tool, MCPTool)],
        **kwargs,
    )
    channel = MCPChannel.from_server_configs({})
    channel._servers["notes"] = MCPServerState(tools=remote, connected=True)
    channel.bind_agent(agent)
    return agent


@pytest.mark.asyncio
async def test_multiple_mcp_sources_share_discovery_and_stop_removes_only_the_closed_sources(
    runtime: Runtime,
) -> None:
    framework, provider, remote, calls = runtime
    archive = MCPTool.from_callable(
        lambda path: f"archived {path}", name="mcp.archive_lookup"
    )
    agent = Agent(framework, tools=[], skill_dirs=[])
    first = MCPChannel.from_server_configs({})
    first._servers["notes"] = MCPServerState(tools=[remote], connected=True)
    second = MCPChannel.from_server_configs({})
    second._servers["archive"] = MCPServerState(tools=[archive], connected=True)
    first.bind_agent(agent)
    second.bind_agent(agent)
    provider.replies = [
        ("mcp_describe", {"names": ["mcp_notes_lookup", "mcp_archive_lookup"]}),
        ("mcp_notes_lookup", {"path": "note"}),
        "found note",
    ]
    try:
        assert await run(agent, "notes") == "found note"
        assert calls == ["note"]
        assert "mcp_notes_lookup" in system_prompt(provider.requests[0])
        assert "mcp_archive_lookup" in system_prompt(provider.requests[0])
        assert definitions(provider.requests[1]).keys() == {
            "mcp_describe",
            "mcp_notes_lookup",
            "mcp_archive_lookup",
        }
        await first.stop()
        provider.requests.clear()
        provider.replies = [("mcp_archive_lookup", {"path": "note"}), "archived note"]
        assert await run(agent, "notes") == "archived note"
        assert definitions(provider.requests[0]).keys() == {
            "mcp_describe",
            "mcp_archive_lookup",
        }
        assert any(
            message.get("content") == "archived note"
            for message in provider.requests[-1]["messages"]
        )
        await second.stop()
        provider.requests.clear()
        provider.replies = ["no sources"]
        assert await run(agent, "notes") == "no sources"
        assert definitions(provider.requests[0]) == {}
        assert "mcp_tools" not in system_prompt(provider.requests[0])
    finally:
        await first.stop()
        await second.stop()
