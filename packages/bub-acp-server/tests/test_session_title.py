"""Tests for automatic session titles (`_meta.lody.sessionTitle`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from acp.schema import Implementation, TextContentBlock
from bub.model_selection import ModelOptions
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.turn import TurnResult

from bub_acp_server.agent import BubACPAgent, _clean_title, is_lody_client


@pytest.fixture(autouse=True)
def isolated_bub_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path / ".bub"))


class FakeClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, object]] = []

    async def session_update(
        self, session_id: str, update: object, **kwargs: Any
    ) -> None:
        del kwargs
        self.updates.append((session_id, update))


class FakeFramework:
    def get_agent_hooks(self):
        return None

    def __init__(self) -> None:
        self.workspace = Path.cwd()
        self.messages: list[Any] = []

    def bind_channel_router(self, router: object) -> None:
        del router

    def get_tape_store(self) -> None:
        return None

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
        del stream_output
        self.messages.append(inbound)
        return TurnResult(
            session_id=inbound.session_id,
            prompt=inbound.content,
            model_output="done",
        )


class TitleStreamAgent:
    """Stand-in runtime agent that answers title prompts with fixed text."""

    def __init__(self, text: str = "Fix login redirect") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    async def run_stream(self, **kwargs: Any) -> AsyncStreamEvents:
        self.calls.append(kwargs)

        async def stream() -> AsyncIterator[StreamEvent]:
            yield StreamEvent("text", {"delta": self.text})

        return AsyncStreamEvents(stream(), state=StreamState())


async def make_session(agent: BubACPAgent) -> Any:
    """Create one ACP session and return its server-side record."""

    response = await agent.new_session(cwd=str(Path.cwd()))
    return agent._sessions[response.session_id]


async def wait_for_updates(client: FakeClient, name: str) -> list[Any]:
    async with asyncio.timeout(1):
        while not any(update.session_update == name for _, update in client.updates):
            await asyncio.sleep(0)
    return [update for _, update in client.updates if update.session_update == name]


def lody_info(**overrides: Any) -> Implementation:
    fields: dict[str, Any] = {
        "name": "lody",
        "title": "Lody",
        "version": "1",
    }
    return Implementation(**(fields | overrides))


@pytest.mark.parametrize(
    ("client_info", "expected"),
    [
        (Implementation(name="lody", title="Lody", version="1"), True),
        (Implementation(name="Lody", title="Lody", version="1"), True),
        (Implementation(name="lody-cli", version="0.5.0"), True),
        (Implementation(name="LODY-desktop", version="1"), True),
        (Implementation(name="lody", version="1"), True),
        (Implementation(name="zed", version="0.200.0"), False),
        (Implementation(name="Zed", title="Zed", version="1"), False),
        (
            Implementation(name="claude-code", title="Claude Code", version="2.0.0"),
            False,
        ),
        (Implementation(name="not-lody", version="1"), False),
        (Implementation(name="mylody", version="1"), False),
        (None, False),
    ],
)
def test_is_lody_client(client_info: Implementation | None, expected: bool) -> None:
    assert is_lody_client(client_info) is expected


@pytest.mark.asyncio
async def test_session_titles_are_advertised_only_to_lody_clients() -> None:
    """The capability is a promise: it only goes to clients that can keep it."""

    for client_info, expected in (
        (lody_info(), True),
        (Implementation(name="zed", title="Zed", version="1"), False),
        (None, False),
    ):
        agent = BubACPAgent(FakeFramework())
        response = await agent.initialize(protocol_version=1, client_info=client_info)
        assert response.agent_capabilities is not None
        assert agent._lody_client is expected
        if not expected:
            # Nothing Lody-namespaced goes to a non-Lody client, including
            # capabilities that are otherwise negotiated per-connection.
            assert response.agent_capabilities.field_meta is None
            continue
        assert response.agent_capabilities.field_meta is not None
        assert response.agent_capabilities.field_meta == {
            "lody": {
                "steering": {
                    "version": 1,
                    "transport": "request",
                    "upstreamTurn": "same",
                    "configPolicy": "active",
                },
                "subagentEvents": {"version": 1},
                "sessionTitle": {"version": 1},
            }
        }


@pytest.mark.asyncio
async def test_non_lody_clients_never_generate_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def generate(self: BubACPAgent, session: Any, request: str) -> str:
        del self, session
        calls.append(request)
        return "Unexpected"

    monkeypatch.setattr(BubACPAgent, "_generate_session_title", generate)
    client = FakeClient()
    agent = BubACPAgent(FakeFramework())
    agent.on_connect(client)
    await agent.initialize(
        protocol_version=1,
        client_info=Implementation(name="zed", title="Zed", version="1"),
    )
    session = await make_session(agent)

    await agent.prompt(
        [TextContentBlock(type="text", text="please fix the login redirect")],
        session.session_id,
    )
    await asyncio.sleep(0)

    assert calls == []
    assert session.title is None
    assert [
        update
        for _, update in client.updates
        if update.session_update == "session_info_update"
    ] == []


@pytest.mark.asyncio
async def test_first_prompt_publishes_and_persists_a_generated_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def generate(self: BubACPAgent, session: Any, request: str) -> str:
        del self, session
        seen.append(request)
        return '"Fix the login redirect."'

    monkeypatch.setattr(BubACPAgent, "_generate_session_title", generate)
    framework = FakeFramework()
    client = FakeClient()
    agent = BubACPAgent(framework)
    agent.on_connect(client)
    await agent.initialize(protocol_version=1, client_info=lody_info())
    session = await make_session(agent)

    await agent.prompt(
        [TextContentBlock(type="text", text="please fix the login redirect")],
        session.session_id,
    )

    pushes = await wait_for_updates(client, "session_info_update")
    assert len(pushes) == 1
    assert pushes[0].title == "Fix the login redirect."
    assert pushes[0].field_meta == {"lody": {"titleSource": "generated"}}
    assert seen == ["please fix the login redirect"]

    fresh = BubACPAgent(framework)
    listed = (await fresh.list_sessions()).sessions
    assert [item.title for item in listed] == ["Fix the login redirect."]


@pytest.mark.asyncio
async def test_titles_are_generated_once_per_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def generate(self: BubACPAgent, session: Any, request: str) -> str:
        del self, session
        calls.append(request)
        return "One title"

    monkeypatch.setattr(BubACPAgent, "_generate_session_title", generate)
    client = FakeClient()
    agent = BubACPAgent(FakeFramework())
    agent.on_connect(client)
    await agent.initialize(protocol_version=1, client_info=lody_info())
    session = await make_session(agent)

    for text in ("first prompt", "second prompt"):
        await agent.prompt(
            [TextContentBlock(type="text", text=text)], session.session_id
        )

    await wait_for_updates(client, "session_info_update")
    assert calls == ["first prompt"]
    assert session.title == "One title"


@pytest.mark.asyncio
async def test_loaded_sessions_keep_their_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def generate(self: BubACPAgent, session: Any, request: str) -> str:
        del self, session
        calls.append(request)
        return "Regenerated"

    monkeypatch.setattr(BubACPAgent, "_generate_session_title", generate)
    client = FakeClient()
    agent = BubACPAgent(FakeFramework())
    agent.on_connect(client)
    await agent.initialize(protocol_version=1, client_info=lody_info())
    session = await make_session(agent)
    session.title = "Existing title"
    agent._save_sessions()

    await agent.prompt(
        [TextContentBlock(type="text", text="hello")], session.session_id
    )

    assert calls == []
    assert [
        update
        for _, update in client.updates
        if update.session_update == "session_info_update"
    ] == []
    assert session.title == "Existing title"


@pytest.mark.asyncio
async def test_title_generation_failure_pushes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def generate(self: BubACPAgent, session: Any, request: str) -> str:
        del self, session, request
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(BubACPAgent, "_generate_session_title", generate)
    client = FakeClient()
    agent = BubACPAgent(FakeFramework())
    agent.on_connect(client)
    await agent.initialize(protocol_version=1, client_info=lody_info())
    session = await make_session(agent)

    await agent.prompt(
        [TextContentBlock(type="text", text="hello")], session.session_id
    )
    await asyncio.sleep(0)

    assert [
        update
        for _, update in client.updates
        if update.session_update == "session_info_update"
    ] == []
    assert session.title is None


@pytest.mark.asyncio
async def test_empty_generation_pushes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def generate(self: BubACPAgent, session: Any, request: str) -> str:
        del self, session, request
        return "   "

    monkeypatch.setattr(BubACPAgent, "_generate_session_title", generate)
    client = FakeClient()
    agent = BubACPAgent(FakeFramework())
    agent.on_connect(client)
    await agent.initialize(protocol_version=1, client_info=lody_info())
    session = await make_session(agent)

    await agent.prompt(
        [TextContentBlock(type="text", text="hello")], session.session_id
    )
    await asyncio.sleep(0)

    assert session.title is None
    assert [
        update
        for _, update in client.updates
        if update.session_update == "session_info_update"
    ] == []


@pytest.mark.asyncio
async def test_generate_session_title_uses_a_lean_throwaway_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = BubACPAgent(FakeFramework())
    session = await make_session(agent)
    session.runtime["model"] = "provider:model"
    stub = TitleStreamAgent()
    agent._runtime_agents[session.session_id] = stub

    title = await agent._generate_session_title(session, "fix the login redirect")

    assert title == "Fix login redirect"
    call = stub.calls[0]
    assert call["session_id"].startswith("temp/")
    assert call["session_id"] != session.session_id
    assert call["model"] == "provider:model"
    assert call["allowed_tools"] == set()
    assert call["allowed_skills"] == set()
    assert "fix the login redirect" in call["prompt"]
    assert call["state"]["session_id"] == call["session_id"]
    assert call["state"]["_runtime_workspace"] == str(session.cwd)


@pytest.mark.asyncio
async def test_missing_runtime_agent_skips_generation() -> None:
    agent = BubACPAgent(FakeFramework())
    session = await make_session(agent)

    assert await agent._generate_session_title(session, "hello") == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fix login redirect", "Fix login redirect"),
        ('"Fix login redirect"', "Fix login redirect"),
        ("“Fix login redirect”", "Fix login redirect"),
        ("Title: Fix login redirect", "Fix login redirect"),
        ("  Fix   login\nredirect  ", "Fix login redirect"),
        ("", ""),
        ("   ", ""),
        ("x" * 200, "x" * 80),
    ],
)
def test_clean_title(raw: str, expected: str) -> None:
    assert _clean_title(raw) == expected
