"""Tests for the Lody subagent event extension (`_lody/subagents/event`)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import bub.builtin.tools  # noqa: F401 - importing the builtin tools fills the registry
import pytest
from acp.schema import ClientCapabilities, TextContentBlock
from bub.model_selection import ModelOptions
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tools import REGISTRY, ToolContext

from bub_acp_server.agent import BubACPAgent
from bub_acp_server.subagents import (
    LODY_SUBAGENT_EVENT_METHOD,
    client_supports_subagent_events,
)


@pytest.fixture(autouse=True)
def isolated_bub_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / ".bub"))


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []

    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        del kwargs
        self.updates.append((session_id, update))

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notifications.append((method, params))


class BrokenClient(FakeClient):
    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RuntimeError(f"cannot deliver {method}")


class FakeFramework:
    def get_agent_hooks(self):
        return None

    def __init__(self) -> None:
        self.workspace = Path.cwd()

    def bind_channel_router(self, router: object) -> None:
        del router

    def get_tape_store(self) -> None:
        return None

    async def get_model_options(
        self, *, session_id: str, workspace: Path
    ) -> ModelOptions:
        del session_id, workspace
        return ModelOptions()


class StubAgent:
    """Stand-in for Bub's agent that replays a canned child stream."""

    def __init__(self, events: list[StreamEvent], *, error: Exception | None = None):
        self.events = events
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.tools = {"subagent": REGISTRY["subagent"], "bash": REGISTRY["bash"]}

    async def run_stream(self, **kwargs: Any) -> AsyncStreamEvents:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

        async def stream() -> AsyncIterator[StreamEvent]:
            for event in self.events:
                yield event

        return AsyncStreamEvents(stream(), state=StreamState())


def child_events() -> list[StreamEvent]:
    return [
        StreamEvent("text", {"delta": "work"}),
        StreamEvent("reasoning", {"delta": "thinking"}),
        StreamEvent(
            "tool_call",
            {
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "bash",
                            "arguments": '{"command":"ls"}',
                        },
                    }
                ]
            },
        ),
        StreamEvent("tool_result", {"tool_results": ["file.txt"]}),
        StreamEvent("final", {"text": "work", "ok": True}),
        StreamEvent("text", {"delta": " done"}),
        StreamEvent("usage", {"usage": {"total_tokens": 42}}),
    ]


async def make_tool_session(
    client: FakeClient,
    events: list[StreamEvent],
    *,
    error: Exception | None = None,
    negotiate: bool = True,
    session: str = "temp",
) -> tuple[BubACPAgent, StubAgent, Any]:
    """Install the streaming subagent tool for one session and return it."""

    framework = FakeFramework()
    agent = BubACPAgent(framework)
    agent.on_connect(client)
    capabilities = (
        ClientCapabilities(field_meta={"lody": {"subagentEvents": {"version": 1}}})
        if negotiate
        else ClientCapabilities()
    )
    await agent.initialize(protocol_version=1, client_capabilities=capabilities)
    created = await agent.new_session(cwd=str(Path.cwd()))
    version = {"session": session, "prompt": "delegate this", "model": None}

    runtime_agent = StubAgent(events, error=error)
    acp_session = agent._sessions[created.session_id]
    if negotiate:
        agent._install_streaming_subagent(runtime_agent, acp_session)
    context = ToolContext(
        tape=None,
        state={
            "_runtime_agent": runtime_agent,
            "session_id": f"acp-server:{acp_session.session_id}",
        },
    )
    return agent, runtime_agent, (runtime_agent.tools["subagent"], context, version)


def payloads(client: FakeClient) -> Iterator[dict[str, Any]]:
    return (params for _, params in client.notifications)


def test_client_negotiation_requires_matching_version() -> None:
    assert client_supports_subagent_events(None) is False
    assert client_supports_subagent_events(ClientCapabilities()) is False
    assert (
        client_supports_subagent_events(
            ClientCapabilities(field_meta={"lody": {"subagentEvents": {}}})
        )
        is False
    )
    assert (
        client_supports_subagent_events(
            ClientCapabilities(field_meta={"lody": {"subagentEvents": {"version": 2}}})
        )
        is False
    )
    assert (
        client_supports_subagent_events(
            ClientCapabilities(field_meta={"lody": {"subagentEvents": {"version": 1}}})
        )
        is True
    )
    # Unnegotiated clients never reach the tool, but a raw mapping is equivalent.
    assert (
        client_supports_subagent_events(
            {"_meta": {"lody": {"subagentEvents": {"version": 1}}}}
        )
        is True
    )


def assert_valid_event(payload: dict[str, Any]) -> None:
    """Enforce the wire rules of `isLodySubagentEvent` from acp-extension-core."""

    _assert_no_nulls(payload)
    assert payload["version"] == 1
    assert isinstance(payload["sessionId"], str) and payload["sessionId"]
    assert isinstance(payload["runId"], str) and payload["runId"]
    kind = payload["type"]
    if kind == "snapshot":
        snapshot = payload["snapshot"]
        assert snapshot["state"] in {
            "pending",
            "running",
            "completed",
            "failed",
            "cancelled",
            "unknown",
        }
        support = snapshot["support"]
        assert set(support["stream"]) <= {"text", "thought", "tool", "plan"}
        assert support["progress"] is True
        assert support["cancel"] is False
        assert support["outputRead"] in {"none", "live_tail", "final_tail"}
    elif kind == "progress":
        assert isinstance(payload["progress"], dict)
    elif kind == "output":
        update = payload["update"]
        variant = update["sessionUpdate"]
        assert variant in {
            "agent_message_chunk",
            "agent_thought_chunk",
            "tool_call",
            "tool_call_update",
            "plan",
        }
        if variant == "tool_call":
            assert update["toolCallId"]
            assert update["title"]
            assert update["status"] in {"pending", "in_progress", "completed", "failed"}
        if variant in {"agent_message_chunk", "agent_thought_chunk"}:
            assert update["content"]["type"] == "text"
    else:  # pragma: no cover - guards the test helper itself
        raise AssertionError(f"unknown event type {kind}")


def _assert_no_nulls(value: object, path: str = "$") -> None:
    if value is None:
        raise AssertionError(f"explicit null at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_nulls(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_nulls(item, f"{path}[{index}]")


@pytest.mark.asyncio
async def test_subagent_run_publishes_snapshot_output_and_progress() -> None:
    client = FakeClient()
    _, runtime_agent, (tool, context, version) = await make_tool_session(
        client, child_events()
    )

    result = await tool.run(context=context, **version)

    assert client.notifications
    assert {method for method, _ in client.notifications} == {
        LODY_SUBAGENT_EVENT_METHOD
    }
    events = list(payloads(client))
    for payload in events:
        assert_valid_event(payload)

    assert events[0]["sessionId"] == context.state["session_id"].split(":", 1)[1]
    assert len({payload["runId"] for payload in events}) == 1

    assert events[0]["type"] == "snapshot"
    assert events[0]["snapshot"]["state"] == "running"
    assert events[0]["snapshot"]["description"] == "delegate this"
    assert events[0]["snapshot"]["support"]["stream"] == ["text", "thought", "tool"]

    assert [event["type"] for event in events] == [
        "snapshot",
        "output",
        "output",
        "output",
        "progress",
        "output",
        "output",
        "progress",
        "snapshot",
    ]
    assert events[1]["update"] == {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "work"},
    }
    assert events[2]["update"]["sessionUpdate"] == "agent_thought_chunk"
    assert events[3]["update"]["sessionUpdate"] == "tool_call"
    assert events[3]["update"]["toolCallId"] == "call-1"
    assert events[3]["update"]["title"] == "ls"
    assert events[3]["update"]["kind"] == "execute"
    assert events[3]["update"]["rawInput"] == {"command": "ls"}
    assert events[4]["progress"]["lastToolName"] == "bash"
    assert events[4]["progress"]["toolCallCount"] == 1
    assert events[5]["update"]["sessionUpdate"] == "tool_call_update"
    assert events[5]["update"]["toolCallId"] == "call-1"
    assert events[5]["update"]["status"] == "completed"
    assert events[5]["update"]["content"][0]["content"]["text"] == "file.txt"
    assert events[6]["update"]["content"]["text"] == " done"
    assert events[7]["progress"] == {
        "lastToolName": "bash",
        "turnCount": 1,
        "toolCallCount": 1,
        "durationMs": events[7]["progress"]["durationMs"],
        "totalTokens": 42,
    }
    assert events[8]["snapshot"]["state"] == "completed"
    assert events[8]["snapshot"]["summary"] == "work done"

    # The tool still returns Bub's structured result to the model.
    assert result["output"] == "work done"
    assert result["errors"] == []
    assert result["session_id"].startswith("temp/")
    assert runtime_agent.calls[0]["session_id"] == result["session_id"]
    assert runtime_agent.calls[0]["prompt"] == "delegate this"


@pytest.mark.asyncio
async def test_subagent_inherit_session_reuses_parent_session() -> None:
    client = FakeClient()
    _, runtime_agent, (tool, context, version) = await make_tool_session(
        client, [StreamEvent("text", {"delta": "ok"})], session="inherit"
    )

    result = await tool.run(context=context, **version)

    assert result["session_id"] == context.state["session_id"]
    assert runtime_agent.calls[0]["session_id"] == context.state["session_id"]


@pytest.mark.asyncio
async def test_subagent_failure_reports_terminal_snapshot_and_raises() -> None:
    client = FakeClient()
    _, _, (tool, context, version) = await make_tool_session(
        client, [], error=RuntimeError("child exploded")
    )

    with pytest.raises(RuntimeError, match="child exploded"):
        await tool.run(context=context, **version)

    events = list(payloads(client))
    for payload in events:
        assert_valid_event(payload)
    assert [event["type"] for event in events] == ["snapshot", "snapshot"]
    assert events[-1]["snapshot"]["state"] == "failed"
    assert events[-1]["snapshot"]["reason"] == {
        "code": "error",
        "message": "child exploded",
    }


@pytest.mark.asyncio
async def test_subagent_stream_errors_are_collected_without_failing_the_run() -> None:
    client = FakeClient()
    events = [
        StreamEvent("error", {"message": "tool blew up"}),
        StreamEvent("text", {"delta": "partial"}),
    ]
    _, _, (tool, context, version) = await make_tool_session(client, events)

    result = await tool.run(context=context, **version)

    assert result["errors"] == ["tool blew up"]
    assert result["output"] == "partial"
    assert list(payloads(client))[-1]["snapshot"]["state"] == "completed"


@pytest.mark.asyncio
async def test_broken_client_does_not_fail_the_tool() -> None:
    client = BrokenClient()
    _, _, (tool, context, version) = await make_tool_session(
        client, [StreamEvent("text", {"delta": "ok"})]
    )

    result = await tool.run(context=context, **version)

    assert result["output"] == "ok"


@pytest.mark.asyncio
async def test_subagent_tool_is_untouched_without_negotiation() -> None:
    client = FakeClient()
    _, _, (tool, context, version) = await make_tool_session(
        client, child_events(), negotiate=False
    )

    assert tool.handler is REGISTRY["subagent"].handler
    assert client.notifications == []
    assert context.state["_runtime_agent"].calls == []


@pytest.mark.asyncio
async def test_negotiated_session_replaces_only_this_session_tool() -> None:
    client = FakeClient()
    framework = FakeFramework()
    agent = BubACPAgent(framework)
    agent.on_connect(client)
    negotiated = await agent.initialize(
        protocol_version=1,
        client_capabilities=ClientCapabilities(
            field_meta={"lody": {"subagentEvents": {"version": 1}}}
        ),
    )
    assert negotiated.agent_capabilities is not None
    # This connection negotiated subagent events but never identified itself as
    # Lody, so nothing Lody-namespaced is reported. The client's own opt-in is
    # still honored: the contract is bilateral and does not require the agent to
    # have advertised first.
    assert negotiated.agent_capabilities.field_meta is None

    session = await agent.new_session(cwd=str(Path.cwd()))
    inbound = agent._build_inbound(
        [TextContentBlock(type="text", text="hi")], agent._sessions[session.session_id]
    )
    installed = inbound._runtime_agent.tools["subagent"]
    assert installed.name == REGISTRY["subagent"].name
    assert installed.description == REGISTRY["subagent"].description
    assert installed.parameters == REGISTRY["subagent"].parameters
    assert installed.renderer is REGISTRY["subagent"].renderer
    assert installed.exposure == REGISTRY["subagent"].exposure
    assert installed.output_schema == REGISTRY["subagent"].output_schema
    assert installed.handler is not REGISTRY["subagent"].handler
    assert REGISTRY["subagent"].handler is not installed.handler

    plain = BubACPAgent(FakeFramework())
    plain.on_connect(FakeClient())
    await plain.initialize(protocol_version=1, client_capabilities=ClientCapabilities())
    other = await plain.new_session(cwd=str(Path.cwd()))
    other_inbound = plain._build_inbound(
        [TextContentBlock(type="text", text="hi")], plain._sessions[other.session_id]
    )
    assert (
        other_inbound._runtime_agent.tools["subagent"].handler
        is REGISTRY["subagent"].handler
    )
