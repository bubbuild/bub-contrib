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

    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        del kwargs
        self.updates.append((session_id, update))


class ControlledFramework:
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
    framework._load_builtin_hooks()
    implementation = ACPServerPlugin(framework)
    framework._plugin_manager.register(implementation, name="acp-server-test")

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
