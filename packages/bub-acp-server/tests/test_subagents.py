"""Tests for the Lody subagent extensions: `_lody/subagents/event` and `subagents`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from acp.exceptions import RequestError
from acp.schema import (
    ClientCapabilities,
    Implementation,
    TextContentBlock,
    ToolCallProgress,
)
from bub.builtin import tools as builtin_tools
from bub.builtin.subagents import run_subagent, subagent_manager, subagent_wait
from bub.core.tools import ToolContext
from bub.errors import BubError, ErrorKind
from bub.model_selection import ModelOptions
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState

from bub_acp_server.agent import BubACPAgent
from bub_acp_server.subagents import (
    LODY_SUBAGENT_EVENT_METHOD,
    client_supports_subagent_events,
)

BUILTIN_TOOLS = {tool.name: tool for tool in builtin_tools.TOOLS}
PARENT_CALL_ID = "call-parent"


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
    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        raise RuntimeError("cannot deliver updates")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RuntimeError(f"cannot deliver {method}")


class FakeFramework:
    def get_agent_hooks(self):
        return SimpleNamespace(subscribe=lambda events: events)

    def build_tape_context(self):
        return None

    def get_tape_sidecars(self):
        return ()

    def get_tools(self):
        from bub.builtin import tools

        return tools.TOOLS

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
    """Stand-in child agent that replays a canned stream, optionally after ``release``."""

    def __init__(
        self,
        events: list[StreamEvent],
        *,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        self.events = events
        self.error = error
        self.block = block
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.calls: list[dict[str, Any]] = []
        self.tools = {
            "subagent": BUILTIN_TOOLS["subagent"],
            "bash": BUILTIN_TOOLS["bash"],
        }

    async def run_stream(self, **kwargs: Any) -> AsyncStreamEvents:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error

        async def stream() -> AsyncIterator[StreamEvent]:
            for event in self.events:
                yield event
                if self.block and event.kind == "text":
                    self.started.set()
                    await self.release.wait()

        return AsyncStreamEvents(stream(), state=StreamState())


def child_events() -> list[StreamEvent]:
    """Two child steps: one reads with a tool, the second answers."""

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
        StreamEvent("final", {"ok": True, "tool_calls": [{"id": "call-1"}]}),
        StreamEvent("text", {"delta": "all done"}),
        StreamEvent("usage", {"usage": {"total_tokens": 42}}),
        StreamEvent("final", {"ok": True, "text": "all done"}),
    ]


async def connect(
    client: FakeClient, *, negotiate: bool = True, lody: bool = False
) -> tuple[BubACPAgent, Any, Any]:
    """Initialize a connection and one session; return the server, session and runtime agent."""

    agent = BubACPAgent(FakeFramework())
    agent.on_connect(client)
    capabilities = (
        ClientCapabilities(field_meta={"lody": {"subagentEvents": {"version": 1}}})
        if negotiate
        else ClientCapabilities()
    )
    await agent.initialize(
        protocol_version=1,
        client_capabilities=capabilities,
        client_info=Implementation(name="lody", version="1.0") if lody else None,
    )
    created = await agent.new_session(cwd=str(Path.cwd()))
    session = agent._sessions[created.session_id]
    inbound = agent._build_inbound([TextContentBlock(type="text", text="hi")], session)
    return agent, session, inbound._runtime_agent


def tool_context(runtime: Any, child: StubAgent, session: Any) -> ToolContext:
    """The context the runtime agent gives its `subagent` call; the child is a stub."""

    return ToolContext(
        tape=None,  # type: ignore[arg-type]
        state={
            "_runtime_agent": child,
            "session_id": f"acp-server:{session.session_id}",
        },
        events=runtime.events,
        call_id=PARENT_CALL_ID,
    )


async def run_child(context: ToolContext, **arguments: Any) -> dict[str, Any]:
    """Start a subagent, which runs in the background, and wait for its result."""

    started = await run_subagent.run(context=context, **arguments)
    [result] = await subagent_wait.run(
        task_ids=[started["task_id"]], timeout_seconds=30, context=context
    )
    return result


def payloads(client: FakeClient) -> Iterator[dict[str, Any]]:
    return (
        params
        for method, params in client.notifications
        if method == LODY_SUBAGENT_EVENT_METHOD
    )


def task_updates(client: FakeClient) -> list[dict[str, Any]]:
    return [
        update.field_meta["lody"]["task"]
        for _, update in client.updates
        if isinstance(update, ToolCallProgress)
        and update.tool_call_id == PARENT_CALL_ID
        and update.field_meta
    ]


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
        assert support["cancel"] is True
        assert support["outputRead"] == "live_tail"
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
    _, session, runtime = await connect(client)
    child = StubAgent(child_events())

    result = await run_child(
        prompt="delegate this", context=tool_context(runtime, child, session)
    )

    events = list(payloads(client))
    for payload in events:
        assert_valid_event(payload)
    assert {payload["sessionId"] for payload in events} == {session.session_id}
    assert len({payload["runId"] for payload in events}) == 1
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
    assert events[0]["snapshot"]["state"] == "running"
    assert events[0]["snapshot"]["description"] == "delegate this"
    assert events[0]["snapshot"]["support"]["stream"] == ["text", "thought", "tool"]
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
    assert events[5]["update"]["status"] == "completed"
    assert events[5]["update"]["content"][0]["content"]["text"] == "file.txt"
    assert events[6]["update"]["content"]["text"] == "all done"
    assert events[7]["progress"] == {
        "lastToolName": "bash",
        "turnCount": 2,
        "toolCallCount": 1,
        "durationMs": events[7]["progress"]["durationMs"],
        "totalTokens": 42,
    }
    assert events[8]["snapshot"]["state"] == "completed"
    assert events[8]["snapshot"]["summary"] == "all done"

    # The tool still returns Bub's result, the child's final answer, to the model.
    assert result["output"] == "all done"
    assert result["status"] == "completed"
    assert child.calls[0]["session_id"] == result["session_id"]


@pytest.mark.asyncio
async def test_subagent_failed_tool_call_is_reported_as_failed() -> None:
    client = FakeClient()
    _, session, runtime = await connect(client)
    error = BubError(ErrorKind.TOOL, "command exited with code 1")
    events = [
        event
        if event.kind != "tool_result"
        else StreamEvent("tool_result", {"tool_results": [error]})
        for event in child_events()
    ]

    await run_child(
        prompt="delegate", context=tool_context(runtime, StubAgent(events), session)
    )

    [update] = [
        payload["update"]
        for payload in payloads(client)
        if payload["type"] == "output"
        and payload["update"]["sessionUpdate"] == "tool_call_update"
    ]
    assert update["status"] == "failed"
    assert "command exited with code 1" in update["content"][0]["content"]["text"]


@pytest.mark.asyncio
async def test_subagent_failure_reports_a_failed_terminal_snapshot() -> None:
    client = FakeClient()
    _, session, runtime = await connect(client)
    child = StubAgent([], error=RuntimeError("child exploded"))

    result = await run_child(tool_context(runtime, child, session), prompt="delegate")

    assert result["status"] == "failed"
    assert result["errors"] == ["child exploded"]

    events = list(payloads(client))
    for payload in events:
        assert_valid_event(payload)
    assert [event["type"] for event in events] == ["snapshot", "progress", "snapshot"]
    assert events[-1]["snapshot"]["state"] == "failed"
    assert events[-1]["snapshot"]["reason"] == {
        "code": "error",
        "message": "child exploded",
    }


@pytest.mark.asyncio
async def test_subagent_stream_errors_are_collected_without_failing_the_run() -> None:
    client = FakeClient()
    _, session, runtime = await connect(client)
    events = [
        StreamEvent("error", {"message": "tool blew up"}),
        StreamEvent("text", {"delta": "partial"}),
    ]

    result = await run_child(
        prompt="delegate", context=tool_context(runtime, StubAgent(events), session)
    )

    assert result["errors"] == ["tool blew up"]
    assert result["output"] == "partial"
    assert list(payloads(client))[-1]["snapshot"]["state"] == "completed"


@pytest.mark.asyncio
async def test_broken_client_does_not_fail_the_subagent() -> None:
    client = BrokenClient()
    _, session, runtime = await connect(client, lody=True)

    result = await run_child(
        prompt="delegate",
        context=tool_context(runtime, StubAgent(child_events()), session),
    )

    assert result["output"] == "all done"


@pytest.mark.asyncio
async def test_nothing_is_published_without_negotiation() -> None:
    client = FakeClient()
    _, session, runtime = await connect(client, negotiate=False)

    result = await run_child(
        prompt="delegate",
        context=tool_context(runtime, StubAgent(child_events()), session),
    )

    assert result["output"] == "all done"
    assert client.notifications == []
    assert task_updates(client) == []
    # The builtin tool is used as is: observation needs no replacement.
    assert runtime.tools["subagent"] is BUILTIN_TOOLS["subagent"]


@pytest.mark.asyncio
async def test_lody_clients_get_the_task_lifecycle_on_the_starting_tool_call() -> None:
    client = FakeClient()
    _, session, runtime = await connect(client, negotiate=False, lody=True)

    await run_child(
        prompt="delegate this",
        context=tool_context(runtime, StubAgent(child_events()), session),
    )

    started, finished = task_updates(client)
    assert started["kind"] == "subagent"
    assert started["status"] == "in_progress"
    assert started["description"] == "delegate this"
    assert finished["taskId"] == started["taskId"]
    assert finished["status"] == "completed"
    assert finished["summary"] == "all done"
    assert finished["lastToolName"] == "bash"
    assert finished["usage"]["toolUses"] == 1
    assert finished["usage"]["totalTokens"] == 42
    assert {session_id for session_id, _ in client.updates} == {session.session_id}


@pytest.mark.asyncio
async def test_subagent_requests_list_read_and_cancel_a_running_task() -> None:
    client = FakeClient()
    agent, session, runtime = await connect(client, lody=True)
    child = StubAgent(child_events(), block=True)

    started = await run_subagent.run(
        prompt="long task",
        context=tool_context(runtime, child, session),
    )
    await child.started.wait()
    [snapshot] = [
        payload for payload in payloads(client) if payload["type"] == "snapshot"
    ]
    task_id = snapshot["runId"]

    listed = await agent.ext_method(
        "lody/subagents/list", {"sessionId": session.session_id}
    )
    assert listed == {
        "tasks": [
            {
                "taskId": task_id,
                "description": "long task",
                "status": "running",
                "startedAtEpochSeconds": listed["tasks"][0]["startedAtEpochSeconds"],
                "endedAtEpochSeconds": None,
            }
        ]
    }
    output = await agent.ext_method(
        "lody/subagents/output", {"sessionId": session.session_id, "taskId": task_id}
    )
    assert output == {"output": "work"}

    assert (
        await agent.ext_method(
            "lody/subagents/cancel",
            {"sessionId": session.session_id, "taskId": task_id},
        )
        == {}
    )

    assert subagent_manager.get(started["task_id"]).status == "cancelled"
    listed = await agent.ext_method(
        "lody/subagents/list", {"sessionId": session.session_id}
    )
    [ended] = listed["tasks"]
    assert ended["status"] == "killed"
    assert isinstance(ended["endedAtEpochSeconds"], int)
    assert await agent.ext_method(
        "lody/subagents/list", {"sessionId": session.session_id, "activeOnly": True}
    ) == {"tasks": []}
    terminal = [p for p in payloads(client) if p["type"] == "snapshot"][-1]
    assert terminal["snapshot"]["state"] == "cancelled"
    assert terminal["snapshot"]["reason"] == {"code": "cancelled"}
    assert task_updates(client)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_subagent_output_request_returns_the_tail() -> None:
    client = FakeClient()
    agent, session, runtime = await connect(client)
    events = [
        StreamEvent("text", {"delta": "line 1\nline 2\nline 3"}),
        StreamEvent("final", {"ok": True, "text": "line 1\nline 2\nline 3"}),
    ]
    await run_child(
        prompt="task", context=tool_context(runtime, StubAgent(events), session)
    )
    [run_id] = {payload["runId"] for payload in payloads(client)}

    output = await agent.ext_method(
        "lody/subagents/output",
        {"sessionId": session.session_id, "taskId": run_id, "tail": 2},
    )

    assert output == {"output": "line 2\nline 3"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"taskId": "missing"},
        {"taskId": "RUN", "sessionId": "other-session"},
        {"taskId": "RUN", "tail": 0},
    ],
)
async def test_subagent_requests_reject_bad_parameters(params: dict[str, Any]) -> None:
    client = FakeClient()
    agent, session, runtime = await connect(client)
    await run_child(
        prompt="task",
        context=tool_context(runtime, StubAgent(child_events()), session),
    )
    [run_id] = {payload["runId"] for payload in payloads(client)}
    request = {"sessionId": session.session_id, **params}
    if request["taskId"] == "RUN":
        request["taskId"] = run_id

    with pytest.raises(RequestError):
        await agent.ext_method("lody/subagents/output", request)


@pytest.mark.asyncio
async def test_closing_a_session_cancels_its_subagents() -> None:
    client = FakeClient()
    agent, session, runtime = await connect(client)
    child = StubAgent(child_events(), block=True)
    started = await run_subagent.run(
        prompt="long task",
        context=tool_context(runtime, child, session),
    )
    await child.started.wait()

    await agent.close_session(session_id=session.session_id)

    assert subagent_manager.get(started["task_id"]).status == "cancelled"
    with pytest.raises(RequestError):
        await agent.ext_method("lody/subagents/list", {"sessionId": ""})
    assert await agent.ext_method(
        "lody/subagents/list", {"sessionId": session.session_id}
    ) == {"tasks": []}
