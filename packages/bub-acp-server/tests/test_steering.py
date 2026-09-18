from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from acp.exceptions import RequestError
from bub.channels.message import ChannelMessage
from bub.framework import BubFramework
from bub.model_selection import ModelOptions
from bub.turn import TurnResult

from bub_acp_server.agent import BubACPAgent
from bub_acp_server.plugin import ACPServerPlugin
from bub_acp_server.steering import ACPSteeringInbox


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []
        self.ext_notifications: list[tuple[str, dict[str, Any]]] = []

    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        del kwargs
        self.updates.append((session_id, update))

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.ext_notifications.append((method, params))


class ControlledFramework:
    def get_agent_hooks(self):
        return None

    def __init__(self, inbox: ACPSteeringInbox) -> None:
        self.workspace = Path.cwd()
        self.inbox = inbox
        self.router: object | None = None
        self.messages: list[Any] = []
        self.entered: asyncio.Queue[int] = asyncio.Queue()
        self.releases: list[asyncio.Event] = []
        self.drain_on_release: set[int] = set()
        self.consumed: list[Any] = []

    def bind_channel_router(self, router: object) -> None:
        self.router = router

    async def quit_via_channel_router(self, session_id: str) -> None:
        del session_id

    async def get_model_options(
        self, *, session_id: str, workspace: Path
    ) -> ModelOptions:
        del session_id, workspace
        return ModelOptions()

    async def process_inbound(
        self, inbound: Any, stream_output: bool = False
    ) -> TurnResult:
        assert stream_output is True
        index = len(self.messages)
        self.messages.append(inbound)
        release = asyncio.Event()
        self.releases.append(release)
        await self.entered.put(index)
        await release.wait()
        if index in self.drain_on_release:
            self.consumed.extend(
                await self.inbox.drain_messages({"session_id": inbound.session_id})
            )
        return TurnResult(
            session_id=inbound.session_id,
            prompt=inbound.content,
            model_output=f"turn-{index}",
        )


@pytest.fixture(autouse=True)
def isolated_bub_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / ".bub"))


async def wait_for_message_count(
    inbox: ACPSteeringInbox, state: dict[str, str], expected: int
) -> None:
    async with asyncio.timeout(1):
        while inbox.message_count(state) != expected:
            await asyncio.sleep(0)


def steering_params(session_id: str, text: str) -> dict[str, object]:
    return {
        "sessionId": session_id,
        "prompt": [{"type": "text", "text": text}],
    }


def acknowledged_steering_params(
    session_id: str, text: str, steer_id: str = "steer-id"
) -> dict[str, object]:
    return {
        **steering_params(session_id, text),
        "steerId": steer_id,
    }


@pytest.mark.asyncio
async def test_steering_inbox_receipt_distinguishes_delivery_from_claim() -> None:
    inbox = ACPSteeringInbox()
    state = {"session_id": "session"}
    delivered = await inbox.enqueue_with_receipt({"content": "delivered"}, state)

    messages = await inbox.drain_messages(state)

    assert [message["content"] for message in messages] == ["delivered"]
    assert delivered.delivered.done()
    assert await inbox.claim_pending(delivered) is None

    pending = await inbox.enqueue_with_receipt({"content": "pending"}, state)
    claimed = await inbox.claim_pending(pending)

    assert claimed is not None
    assert claimed["content"] == "pending"
    assert not pending.delivered.done()
    assert inbox.message_count(state) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_first", [False, True])
async def test_steering_claim_and_drain_deliver_each_message_once(
    claim_first: bool,
) -> None:
    inbox = ACPSteeringInbox()
    state = {"session_id": "session"}
    message = {"content": "same message"}
    first = await inbox.enqueue_with_receipt(message, state)
    second = await inbox.enqueue_with_receipt(message, state)
    await inbox.enqueue_message({"content": "another session"}, {"session_id": "other"})
    operations = [inbox.claim_pending(first), inbox.drain_messages(state)]
    if not claim_first:
        operations.reverse()
    results = await asyncio.gather(*operations)
    claimed, drained = results if claim_first else reversed(results)

    assert len(drained) + (claimed is not None) == 2
    assert first.delivered.done() is not claim_first
    assert second.delivered.done()
    assert inbox.message_count(state) == 0
    assert await inbox.drain_messages({"session_id": "other"}) == [
        {"content": "another session"}
    ]


@pytest.mark.asyncio
async def test_active_turn_consumes_steering_and_reports_injected(
    tmp_path: Path,
) -> None:
    inbox = ACPSteeringInbox()
    framework = ControlledFramework(inbox)
    framework.drain_on_release.add(0)
    agent = BubACPAgent(cast(Any, framework), steering_inbox=inbox)
    agent.on_connect(cast(Any, FakeClient()))
    session = await agent.new_session(cwd=str(tmp_path))
    internal_state = {"session_id": f"acp-server:{session.session_id}"}

    prompt_task = asyncio.create_task(
        agent.prompt(
            [{"type": "text", "text": "initial"}],
            session_id=session.session_id,
        )
    )
    assert await framework.entered.get() == 0
    steer_task = asyncio.create_task(
        agent.ext_method(
            "session/steering", steering_params(session.session_id, "change course")
        )
    )
    await wait_for_message_count(inbox, internal_state, 1)

    framework.releases[0].set()

    assert await steer_task == {"outcome": "injected"}
    await prompt_task
    assert [message.content for message in framework.consumed] == ["change course"]
    assert len(framework.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "applied_method"),
    [
        ("lody/session/steer", "lody/session/steer_applied"),
        ("session/steering", "session/steering_applied"),
    ],
)
async def test_acknowledged_steering_notifies_after_model_step_consumes_it(
    tmp_path: Path, method: str, applied_method: str
) -> None:
    inbox = ACPSteeringInbox()
    framework = ControlledFramework(inbox)
    framework.drain_on_release.add(0)
    client = FakeClient()
    agent = BubACPAgent(cast(Any, framework), steering_inbox=inbox)
    agent.on_connect(cast(Any, client))
    session = await agent.new_session(cwd=str(tmp_path))

    prompt_task = asyncio.create_task(
        agent.prompt(
            [{"type": "text", "text": "initial"}],
            session_id=session.session_id,
        )
    )
    assert await framework.entered.get() == 0
    steer_task = asyncio.create_task(
        agent.ext_method(
            method,
            acknowledged_steering_params(
                session.session_id, "change course", "steer-1"
            ),
        )
    )
    await wait_for_message_count(
        inbox, {"session_id": f"acp-server:{session.session_id}"}, 1
    )
    assert client.ext_notifications == []

    framework.releases[0].set()

    assert await steer_task == {"outcome": "injected"}
    assert client.ext_notifications == [
        (
            applied_method,
            {"sessionId": session.session_id, "steerId": "steer-1"},
        )
    ]
    await prompt_task
    assert [message.content for message in framework.consumed] == ["change course"]


@pytest.mark.asyncio
async def test_steering_acknowledges_consumption_before_turn_finishes(
    tmp_path: Path,
) -> None:
    inbox = ACPSteeringInbox()
    framework = ControlledFramework(inbox)
    agent = BubACPAgent(cast(Any, framework), steering_inbox=inbox)
    agent.on_connect(cast(Any, FakeClient()))
    session = await agent.new_session(cwd=str(tmp_path))
    state = {"session_id": f"acp-server:{session.session_id}"}
    prompt_task = asyncio.create_task(
        agent.prompt(
            [{"type": "text", "text": "initial"}],
            session.session_id,
        )
    )
    await framework.entered.get()
    steer_task = asyncio.create_task(
        agent.ext_method(
            "lody/session/steer",
            acknowledged_steering_params(session.session_id, "steer"),
        )
    )
    try:
        await wait_for_message_count(inbox, state, 1)
        assert len(await inbox.drain_messages(state)) == 1
        async with asyncio.timeout(1):
            assert await steer_task == {"outcome": "injected"}
        assert not prompt_task.done()
    finally:
        framework.releases[0].set()
        await prompt_task
        if not steer_task.done():
            steer_task.cancel()
        await asyncio.gather(steer_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_steering_starts_background_turn_without_dropping_prompt(
    tmp_path: Path,
) -> None:
    inbox = ACPSteeringInbox()
    framework = ControlledFramework(inbox)
    agent = BubACPAgent(cast(Any, framework), steering_inbox=inbox)
    agent.on_connect(cast(Any, FakeClient()))
    session = await agent.new_session(cwd=str(tmp_path))

    prompt_task = asyncio.create_task(
        agent.prompt(
            [{"type": "text", "text": "initial"}],
            session_id=session.session_id,
        )
    )
    assert await framework.entered.get() == 0
    steer_task = asyncio.create_task(
        agent.ext_method(
            "session/steering", steering_params(session.session_id, "late steer")
        )
    )
    await wait_for_message_count(
        inbox, {"session_id": f"acp-server:{session.session_id}"}, 1
    )

    framework.releases[0].set()
    await prompt_task
    assert await framework.entered.get() == 1

    assert await steer_task == {"outcome": "startedNewTurn"}
    assert [message.content for message in framework.messages] == [
        "initial",
        "late steer",
    ]
    assert inbox.message_count({"session_id": f"acp-server:{session.session_id}"}) == 0

    framework.releases[1].set()
    await asyncio.gather(*agent._background_tasks)


@pytest.mark.asyncio
async def test_idle_steering_returns_after_background_turn_starts(
    tmp_path: Path,
) -> None:
    inbox = ACPSteeringInbox()
    framework = ControlledFramework(inbox)
    agent = BubACPAgent(cast(Any, framework), steering_inbox=inbox)
    agent.on_connect(cast(Any, FakeClient()))
    session = await agent.new_session(cwd=str(tmp_path))

    steer_task = asyncio.create_task(
        agent.ext_method(
            "session/steering", steering_params(session.session_id, "start work")
        )
    )
    assert await framework.entered.get() == 0

    assert await steer_task == {"outcome": "startedNewTurn"}
    assert not framework.releases[0].is_set()

    framework.releases[0].set()
    await asyncio.gather(*agent._background_tasks)


@pytest.mark.asyncio
async def test_concurrent_late_steers_are_serialized_without_dropping_input(
    tmp_path: Path,
) -> None:
    inbox = ACPSteeringInbox()
    framework = ControlledFramework(inbox)
    framework.drain_on_release.add(1)
    agent = BubACPAgent(cast(Any, framework), steering_inbox=inbox)
    agent.on_connect(cast(Any, FakeClient()))
    session = await agent.new_session(cwd=str(tmp_path))
    internal_state = {"session_id": f"acp-server:{session.session_id}"}

    prompt_task = asyncio.create_task(
        agent.prompt(
            [{"type": "text", "text": "initial"}],
            session_id=session.session_id,
        )
    )
    assert await framework.entered.get() == 0
    first = asyncio.create_task(
        agent.ext_method(
            "session/steering", steering_params(session.session_id, "first steer")
        )
    )
    second = asyncio.create_task(
        agent.ext_method(
            "session/steering", steering_params(session.session_id, "second steer")
        )
    )
    await wait_for_message_count(inbox, internal_state, 1)

    framework.releases[0].set()
    await prompt_task
    assert await framework.entered.get() == 1
    assert await first == {"outcome": "startedNewTurn"}
    await wait_for_message_count(inbox, internal_state, 1)

    framework.releases[1].set()

    assert await second == {"outcome": "injected"}
    assert [message.content for message in framework.messages] == [
        "initial",
        "first steer",
    ]
    assert [message.content for message in framework.consumed] == ["second steer"]
    await asyncio.gather(*agent._background_tasks)


@pytest.mark.asyncio
async def test_steer_waits_for_pending_prompt_then_injects(
    tmp_path: Path,
) -> None:
    inbox = ACPSteeringInbox()
    framework = ControlledFramework(inbox)
    framework.drain_on_release.add(0)
    agent = BubACPAgent(cast(Any, framework), steering_inbox=inbox)
    agent.on_connect(cast(Any, FakeClient()))
    session = await agent.new_session(cwd=str(tmp_path))
    internal_state = {"session_id": f"acp-server:{session.session_id}"}
    await agent._prompt_lock.acquire()

    prompt_task = asyncio.create_task(
        agent.prompt(
            [{"type": "text", "text": "pending prompt"}],
            session_id=session.session_id,
        )
    )
    await asyncio.sleep(0)
    steer_task = asyncio.create_task(
        agent.ext_method(
            "session/steering", steering_params(session.session_id, "pending steer")
        )
    )
    await asyncio.sleep(0)
    assert not steer_task.done()

    agent._prompt_lock.release()
    assert await framework.entered.get() == 0
    await wait_for_message_count(inbox, internal_state, 1)
    framework.releases[0].set()

    assert await steer_task == {"outcome": "injected"}
    await prompt_task
    assert [message.content for message in framework.consumed] == ["pending steer"]
    assert len(framework.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("task_entered", [False, True])
async def test_closing_session_cancels_queued_background_turn(
    tmp_path: Path,
    task_entered: bool,
) -> None:
    framework = ControlledFramework(ACPSteeringInbox())
    agent = BubACPAgent(cast(Any, framework), steering_inbox=framework.inbox)
    agent.on_connect(cast(Any, FakeClient()))
    session = await agent.new_session(cwd=str(tmp_path))
    await agent._prompt_lock.acquire()
    steer_task = asyncio.create_task(
        agent.ext_method(
            "session/steering",
            steering_params(session.session_id, "queued work"),
        )
    )
    try:
        async with asyncio.timeout(1):
            while agent._current_prompt_run(session.session_id) is None:
                await asyncio.sleep(0)
            if task_entered:
                await asyncio.sleep(0)
            await agent.close_session(session.session_id)
            with pytest.raises(asyncio.CancelledError):
                await steer_task
        assert framework.messages == []
        assert agent._prompt_runs == {}
    finally:
        agent._prompt_lock.release()
        if not steer_task.done():
            steer_task.cancel()
        await asyncio.gather(steer_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_steering_rejects_malformed_and_unknown_sessions(
    tmp_path: Path,
) -> None:
    agent = BubACPAgent(cast(Any, ControlledFramework(ACPSteeringInbox())))
    await agent.new_session(cwd=str(tmp_path))

    with pytest.raises(RequestError) as malformed:
        await agent.ext_method(
            "session/steering", {"sessionId": "session", "prompt": []}
        )
    assert malformed.value.code == -32602

    with pytest.raises(RequestError) as unknown:
        await agent.ext_method("session/steering", steering_params("missing", "hello"))
    assert unknown.value.code == -32602


@pytest.mark.asyncio
async def test_unexpected_steering_failure_returns_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = BubACPAgent(cast(Any, ControlledFramework(ACPSteeringInbox())))

    async def fail(*args: object) -> dict[str, object]:
        del args
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "_execute_or_queue_steering", fail)

    response = await agent.ext_method(
        "session/steering", steering_params("session", "hello")
    )

    assert response == {"outcome": "failed"}


def test_plugin_provides_receipt_aware_steering_inbox() -> None:
    implementation = ACPServerPlugin(cast(Any, object()))

    assert implementation.provide_steering_inbox() is implementation.steering_inbox


@pytest.mark.asyncio
async def test_plugin_steering_inbox_precedes_builtin_provider(
    tmp_path: Path,
) -> None:
    framework = BubFramework(config_file=tmp_path / "config.yml")
    framework.load_builtin_hooks()
    implementation = ACPServerPlugin(framework)
    framework.plugin_manager.register(implementation, name="acp-server-test")

    async with framework.running():
        assert framework.get_steering_inbox() is implementation.steering_inbox
        state = await framework.build_state(
            ChannelMessage(
                session_id="acp-server:session",
                channel="acp-server",
                chat_id="session",
                content="hello",
                context={
                    "_runtime_workspace": str(tmp_path),
                    "_runtime_reasoning_effort": "high",
                },
            ),
            "acp-server:session",
        )

    assert state["_runtime_workspace"] == str(tmp_path)
    assert state["reasoning_effort"] == "high"
