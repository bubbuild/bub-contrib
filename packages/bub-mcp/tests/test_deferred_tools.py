from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from any_llm.types.completion import ChatCompletion
from fastmcp import Client, FastMCP

from bub.builtin.agent import Agent
from bub.builtin.codemode import run_code
from bub.framework import BubFramework
from bub.store import FileTapeStore
from bub.tools import REGISTRY, Tool
from bub_mcp import plugin
from bub_mcp.config import MCPSettings
from bub_mcp.plugin import MCPChannel, MCPServerState


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


def tool_results(request: dict[str, Any]) -> str:
    return "\n".join(
        message.get("content", "")
        for message in request["messages"]
        if message["role"] == "tool"
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
        "bub.builtin.model_runner.AnyLLM.create", lambda *a, **k: provider
    )
    calls: list[str] = []

    def lookup(path: str) -> str:
        calls.append(path)
        return f"found {path}"

    remote = Tool.from_callable(
        lookup,
        name="mcp.notes_lookup",
        description="Read a remote note. Detailed instructions stay in the native definition.",
    )
    return framework, provider, remote, calls


def make_agent(framework: BubFramework, *, tools: list[Tool], **kwargs: Any) -> Agent:
    agent = Agent(
        framework,
        tools=[item for item in tools if not item.name.startswith("mcp.")],
        skill_dirs=[],
        **kwargs,
    )
    channel = MCPChannel.from_server_configs({})
    channel._servers["notes"] = MCPServerState(
        tools=[item for item in tools if item.name.startswith("mcp.")], connected=True
    )
    channel.bind_agent(agent)
    return agent


async def run(agent: Agent, session: str = "notes", **kwargs: Any) -> str:
    stream = await agent.run_stream(
        session_id=session,
        prompt="Read a note.",
        model="openrouter:test-model",
        **kwargs,
    )
    return "".join(
        [event.data.get("text", "") async for event in stream if event.kind == "final"]
    )


async def test_builtins_are_direct_and_only_described_mcp_tools_become_native(
    runtime: Runtime,
) -> None:
    framework, provider, remote, calls = runtime
    other = Tool.from_callable(lambda: "other", name="mcp.notes_other")
    agent = make_agent(framework, tools=[REGISTRY["tape.info"], remote, other])
    provider.replies = [
        ("tape_info", {}),
        ("mcp_describe", {"names": ["mcp_notes_lookup"]}),
        ("mcp_notes_lookup", {"path": "note"}),
        "done",
    ]
    await run(agent)
    assert calls == ["note"]
    initial, after_builtin, loaded, completed = provider.requests
    assert "tape_info" in definitions(initial)
    assert "entries" in tool_results(after_builtin)
    assert "mcp_notes_lookup" in system_prompt(initial)
    assert "Detailed instructions" not in system_prompt(initial)
    assert "mcp_notes_lookup" not in definitions(initial)
    assert "mcp_notes_lookup" in definitions(loaded)
    assert "mcp_notes_other" not in definitions(loaded)
    assert "found note" in tool_results(completed)


async def test_scope_allows_discovery_and_calls_only_for_selected_tools(
    runtime: Runtime,
) -> None:
    framework, provider, remote, calls = runtime
    other = Tool.from_callable(lambda: "other", name="mcp.notes_other")
    agent = make_agent(framework, tools=[remote, other])
    provider.replies = [
        ("mcp_describe", {"names": ["mcp_notes_other"]}),
        ("mcp_describe", {"names": ["mcp_notes_lookup"]}),
        ("mcp_notes_lookup", {"path": "note"}),
        "done",
    ]
    await run(agent, allowed_tools=["mcp_notes_lookup"])
    assert calls == ["note"]
    assert "mcp_describe" in definitions(provider.requests[0])
    assert "mcp_notes_lookup" not in definitions(provider.requests[1])
    assert all(
        "mcp_notes_other" not in definitions(request) for request in provider.requests
    )
    assert "mcp_notes_other" not in system_prompt(provider.requests[0])


async def test_loaded_definitions_survive_restart(
    runtime: Runtime, tmp_path: Path
) -> None:
    framework, provider, remote, calls = runtime
    store = FileTapeStore(tmp_path / "store")
    agent = make_agent(framework, tools=[remote], tape_store=store)
    provider.replies = [("mcp_describe", {"names": ["mcp_notes_lookup"]}), "ready"]
    await run(agent)
    restored = make_agent(
        framework, tools=[remote], tape_store=FileTapeStore(tmp_path / "store")
    )
    provider.requests.clear()
    provider.replies = [("mcp_notes_lookup", {"path": "note"}), "done"]
    await run(restored)
    assert "mcp_notes_lookup" in definitions(provider.requests[0])
    assert calls == ["note"]


async def test_loading_in_one_session_does_not_expose_definitions_in_another(
    runtime: Runtime,
) -> None:
    framework, provider, remote, _ = runtime
    agent = make_agent(framework, tools=[remote])
    provider.replies = [
        ("mcp_describe", {"names": ["mcp_notes_lookup"]}),
        "ready",
        "ready",
    ]
    await run(agent, "first")
    provider.requests.clear()
    await run(agent, "second")
    assert "mcp_notes_lookup" not in definitions(provider.requests[0])
    assert "mcp_notes_lookup" in system_prompt(provider.requests[0])


@pytest.mark.parametrize("clear_context", ["reset", "handoff"])
async def test_clearing_context_requires_discovery_again(
    runtime: Runtime, clear_context: str
) -> None:
    framework, provider, remote, _ = runtime
    agent = make_agent(framework, tools=[remote])
    provider.replies = [
        ("mcp_describe", {"names": ["mcp_notes_lookup"]}),
        "ready",
        "ready",
    ]
    await run(agent)
    tape = agent.tape.session_tape("notes", framework.workspace)
    if clear_context == "reset":
        await tape.reset()
    else:
        await tape.handoff(name="next")
    provider.requests.clear()
    await run(agent)
    assert "mcp_notes_lookup" not in definitions(provider.requests[0])
    assert "mcp_notes_lookup" in system_prompt(provider.requests[0])


async def test_two_mcp_sources_route_same_named_tools_and_close_independently(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    framework, provider, _, _ = runtime
    servers = {name: FastMCP(name) for name in ("notes", "archive")}
    calls: list[tuple[str, str]] = []

    def lookup_on(name: str) -> Callable[[str], str]:
        def lookup(path: str) -> str:
            calls.append((name, path))
            return f"{name}: {path}"

        return lookup

    for name, server in servers.items():
        server.tool(name="lookup")(lookup_on(name))
    monkeypatch.setattr(
        plugin,
        "_create_fastmcp_client",
        lambda config, **k: Client(servers[next(iter(config))]),
    )
    notes, archive = [
        MCPChannel.from_server_configs({name: {"command": name}}) for name in servers
    ]
    agent = Agent(framework, tools=[], skill_dirs=[])
    try:
        for channel in (notes, archive):
            await channel.connect()
            channel.bind_agent(agent)
        provider.replies = [
            ("mcp_describe", {"names": ["mcp_notes_lookup", "mcp_archive_lookup"]}),
            ("mcp_notes_lookup", {"path": "a"}),
            ("mcp_archive_lookup", {"path": "b"}),
            "done",
        ]
        await run(agent)
        assert calls == [("notes", "a"), ("archive", "b")]
        assert "notes: a" in tool_results(provider.requests[-1])
        assert "archive: b" in tool_results(provider.requests[-1])
        await notes.stop()
        provider.requests.clear()
        provider.replies = [("mcp_archive_lookup", {"path": "c"}), "done"]
        await run(agent)
        assert calls[-1] == ("archive", "c")
        assert "mcp_notes_lookup" not in definitions(provider.requests[0])
        await archive.stop()
        provider.requests.clear()
        provider.replies = ["done"]
        await run(agent)
        assert definitions(provider.requests[0]) == {}
    finally:
        await notes.stop()
        await archive.stop()


async def test_code_mode_calls_mcp_without_native_definitions(
    runtime: Runtime,
) -> None:
    framework, provider, remote, calls = runtime
    agent = make_agent(framework, tools=[run_code, remote])
    provider.replies = [
        ("run_code", {"code": "print(await tools.mcp_notes_lookup(path='note'))"}),
        "done",
    ]
    await run(
        agent, state={"code_mode": True, "_runtime_workspace": str(framework.workspace)}
    )
    assert calls == ["note"]
    assert "mcp_notes_lookup" not in definitions(provider.requests[0])
    assert "found note" in tool_results(provider.requests[-1])


@pytest.mark.parametrize(
    ("allowed", "excluded", "visible"),
    [
        (None, [], {"mcp_notes_lookup", "mcp_notes_other"}),
        ([], [], set()),
        (["mcp.notes_*"], ["mcp_notes_other"], {"mcp_notes_lookup"}),
        (["mcp_notes_lookup"], ["mcp.notes_lookup"], set()),
    ],
)
async def test_configured_allow_and_exclude_control_discovery(
    runtime: Runtime, allowed: list[str] | None, excluded: list[str], visible: set[str]
) -> None:
    framework, provider, remote, _ = runtime
    agent = Agent(framework, tools=[], skill_dirs=[])
    channel = MCPChannel.from_server_configs({})
    channel.settings = MCPSettings.model_validate(
        {"allowed_tools": allowed, "excluded_tools": excluded}
    )
    other = Tool.from_callable(lambda: "other", name="mcp.notes_other")
    channel._servers["notes"] = MCPServerState(tools=[remote, other], connected=True)
    channel.bind_agent(agent)
    provider.replies = ["done"]
    await run(agent)
    catalog = system_prompt(provider.requests[0])
    assert {
        name for name in ("mcp_notes_lookup", "mcp_notes_other") if name in catalog
    } == visible


async def test_subagent_can_discover_a_tool_not_loaded_by_its_parent(
    runtime: Runtime,
) -> None:
    framework, provider, remote, calls = runtime
    agent = make_agent(framework, tools=[REGISTRY["subagent"], remote])
    provider.replies = [
        (
            "subagent",
            {
                "prompt": "Read the note in a child session.",
                "allowed_tools": ["mcp_notes_lookup"],
                "model": "openrouter:test-model",
            },
        ),
        ("mcp_describe", {"names": ["mcp_notes_lookup"]}),
        ("mcp_notes_lookup", {"path": "note"}),
        "found note",
        "done",
    ]
    await run(agent, "parent")
    assert any(
        message["role"] == "user"
        and message["content"] == "Read the note in a child session."
        for message in provider.requests[1]["messages"]
    )
    assert calls == ["note"]
    assert "found note" in tool_results(provider.requests[-1])
