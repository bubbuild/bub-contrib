from __future__ import annotations

import json
import os
from collections import deque

import httpx2
import pytest
import republic
from ag_ui.core import RunAgentInput
from bub.builtin.agent import BubAgent
from bub.builtin.settings import AgentSettings
from bub.core.store import InMemoryTapeStore
from bub.framework import BubFramework
from bub_ag_ui.runtime import AGUIAdapter


class Provider:
    def __init__(self):
        self.responses = deque()
        self.requests = []

    def reply(self, text="", calls=()):
        delta = {"content": text}
        if calls:
            delta["tool_calls"] = [
                {"index": i, "id": c[0], "function": {"name": c[1], "arguments": c[2]}}
                for i, c in enumerate(calls)
            ]
        events = [
            {
                "choices": [
                    {"delta": delta, "finish_reason": "tool_calls" if calls else "stop"}
                ]
            },
            "[DONE]",
        ]
        self.responses.append(
            httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(
                    f"data: {json.dumps(e) if not isinstance(e, str) else e}\n\n"
                    for e in events
                ),
            )
        )

    async def respond(self, request):
        self.requests.append(json.loads(request.content))
        if not self.responses:
            raise AssertionError("Unexpected extra model call")
        return self.responses.popleft()


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("BUB_"):
            monkeypatch.delenv(key)
    provider = Provider()
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(provider.respond)
    ) as client:
        model = republic.get_model(
            "openai:test", http_client=client, api_format="chat", api_key="test"
        )
        monkeypatch.setattr(AgentSettings, "chat_model", lambda *args, **kwargs: model)
        framework = BubFramework(config_file=tmp_path / "config.toml")
        framework.load_builtin_hooks()
        store = InMemoryTapeStore()
        adapters = []

        def make(tools=(), *, tape_store=None, **kwargs):
            def factory(*args, **options):
                options.setdefault(
                    "tape_store", tape_store if tape_store is not None else store
                )
                return BubAgent(*args, tools=tools, skill_dirs=[], **options)

            adapter = AGUIAdapter(
                framework, workspace=tmp_path, agent_factory=factory, **kwargs
            )
            adapters.append(adapter)
            return adapter

        yield provider, make
        for adapter in adapters:
            await adapter.close()


def request(messages=None, *, run="r1", thread="t1", tools=None, **kwargs):
    return RunAgentInput(
        thread_id=thread,
        run_id=run,
        state={},
        messages=messages or [{"id": "u1", "role": "user", "content": "Hello"}],
        tools=tools or [],
        context=[],
        forwarded_props={},
        **kwargs,
    )


class ClientView:
    """Test-side reducer, never an adapter history API."""

    def __init__(self, projection, pending):
        self.projection = projection
        self.pending = pending

    async def history(self):
        return {
            "messages": self.projection.messages,
            "pending": self.pending,
        }


async def collect(adapter, data, user="alice"):
    from bub_ag_ui.events import MessageProjection

    events = [event async for event in adapter.run(user, data)]
    projection = MessageProjection()
    for event in events:
        projection.apply(event)
    live = adapter._turns.get(adapter.session_id(user, data.thread_id))
    pending = list(live.inbound.external.pending) if live else []
    return ClientView(projection, pending), events
