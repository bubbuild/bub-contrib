"""The adapter uses the full native stream, including Bub-owned loop policy."""

import httpx2
from bub import hookimpl
from bub.core.events import MessagesPending
from bub.core.tape import session_tape_name
from bub.core.tools import Tool

from .conftest import collect, request


async def test_native_run_prepares_tools_once_and_owns_continue_hook(setup):
    provider, make = setup
    observed = []

    def load(context):
        context.state["_runtime_agent"].tools["later"] = Tool.from_callable(
            lambda: "later", name="later"
        )
        return "loaded"

    adapter = make([Tool.from_callable(load, context=True)])

    class Hooks:
        @hookimpl
        def continue_prompt(self, state):
            observed.append((state.system_prompt, [t.name for t in state.tools]))
            return "Native continue hook"

    adapter.framework.plugin_manager.register(Hooks(), "native-continuation")
    provider.reply(calls=[("load", "load", "{}")])
    provider.reply("done")
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert len(observed) == 1
    assert observed[0][0]  # public LLM request supplies the observed system prompt
    assert observed[0][1] == ["load"]
    assert "later" not in {
        t["function"]["name"] for t in provider.requests[-1]["tools"]
    }
    assert provider.requests[-1]["messages"][-1]["content"] == "Native continue hook"


async def test_native_end_of_step_message_delivery_is_preserved(setup):
    provider, make = setup
    adapter = make()
    delivered = False

    def deliver(event):
        nonlocal delivered
        if event.step_ended and not delivered:
            delivered = True
            event.messages.append(["Native queued input"])

    class Hooks:
        @hookimpl
        def load_state(self, message):
            message._runtime_agent.events.on(MessagesPending, deliver)

    adapter.framework.plugin_manager.register(Hooks(), "native-message-delivery")
    provider.reply("first")
    provider.reply("after queued input")
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert len(provider.requests) == 2
    assert [
        m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "user"
    ] == ["Hello", "Native queued input"]


async def test_native_stream_owns_context_overflow_handoff_and_retry(setup):
    provider, make = setup
    adapter = make()
    provider.responses.append(
        httpx2.Response(
            400,
            json={
                "error": {
                    "message": "maximum context length exceeded",
                    "code": "context_length_exceeded",
                }
            },
        )
    )
    provider.reply("after native handoff")
    _, events = await collect(adapter, request())
    assert len(provider.requests) == 2
    assert events[-1]["type"] == "RUN_FINISHED"
    tape = adapter._storage_agent.tape(
        session_tape_name(adapter.session_id("alice", "t1"), adapter.workspace)
    )
    entries = list(await tape.query().all())
    assert "auto_handoff/context_overflow" in [
        e.payload["name"] for e in entries if e.kind == "anchor"
    ]
    assert any(e.payload.get("name") == "loop.start" for e in entries)
    assert any(e.payload.get("name") == "loop.finish" for e in entries)


async def test_step_limit_still_bounds_backend_tool_loop(setup):
    provider, make = setup
    adapter = make([Tool.from_callable(lambda: "clock", name="clock")])

    class Limit:
        @hookimpl
        def load_state(self, message):
            message._runtime_agent.max_steps = 1

    adapter.framework.plugin_manager.register(Limit(), "one-step")
    provider.reply(calls=[("c1", "clock", "{}")])
    _, events = await collect(adapter, request())
    assert len(provider.requests) == 1
    assert events[-1]["type"] == "RUN_ERROR"
    assert len([e for e in events if e["type"] == "TOOL_CALL_RESULT"]) == 1


async def test_native_preparation_sees_shared_state_updates_on_next_step(setup):
    from bub_ag_ui.state import shared_state

    provider, make = setup

    async def update(context):
        await shared_state(context).set({"count": 1})
        return "updated"

    adapter = make([Tool.from_callable(update, context=True)])
    provider.reply(calls=[("u1", "update", "{}")])
    provider.reply("done")
    data = request()
    data.state = {"count": 0}
    _, events = await collect(adapter, data)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert '"count": 0' in provider.requests[0]["messages"][0]["content"]
    assert '"count": 1' in provider.requests[1]["messages"][0]["content"]


async def test_frontend_attachments_share_one_native_run_and_record_complete_exchange(
    setup, monkeypatch
):
    from bub.builtin.agent import BubAgent
    from bub.core.events import ChatFinished, RunFinished, RunStarted

    provider, make = setup
    native_calls, lifecycle, saved = [], [], []
    original = BubAgent.run_stream

    async def counted(self, **kwargs):
        native_calls.append(kwargs)
        return await original(self, **kwargs)

    monkeypatch.setattr(BubAgent, "run_stream", counted)
    adapter = make()

    class Observe:
        @hookimpl
        def load_state(self, message):
            for event_type in (RunStarted, ChatFinished, RunFinished):
                message._runtime_agent.events.on(event_type, lifecycle.append)

        @hookimpl
        def save_state(self, model_output):
            saved.append(model_output)

    adapter.framework.plugin_manager.register(Observe(), "native-lifecycle")
    tools = [{"name": "browser", "description": "", "parameters": {}}]
    provider.reply(calls=[("f1", "browser", "{}")])
    _, paused = await collect(adapter, request(tools=tools))
    assert paused[-1]["type"] == "RUN_FINISHED"
    assert len(native_calls) == 1
    assert [type(event) for event in lifecycle] == [RunStarted]
    assert saved == []
    tape = adapter._storage_agent.tape(
        session_tape_name(adapter.session_id("alice", "t1"), adapter.workspace)
    )
    assert not await tape.query().kinds("message").all()

    provider.reply("done")
    _, finished = await collect(
        adapter,
        request(
            [
                {
                    "id": "result",
                    "role": "tool",
                    "toolCallId": "f1",
                    "content": "client value",
                }
            ],
            run="r2",
            tools=tools,
        ),
    )
    assert finished[-1]["type"] == "RUN_FINISHED"
    assert len(native_calls) == 1
    assert [type(event) for event in lifecycle] == [
        RunStarted,
        ChatFinished,
        ChatFinished,
        RunFinished,
    ]
    assert saved == ["done"]
    assert [message.role for message in await tape.read_messages()] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert not adapter._turns


async def test_detached_native_turn_timeout_releases_futures(setup):
    import asyncio

    import pytest

    provider, make = setup
    adapter = make(max_run_seconds=0.05)
    tools = [{"name": "browser", "description": "", "parameters": {}}]
    provider.reply(calls=[("f1", "browser", "{}")])
    await collect(adapter, request(tools=tools))
    live = adapter._turns[adapter.session_id("alice", "t1")]
    future = live.inbound.external.futures["f1"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(live.task), 1)
    assert future.cancelled()
    assert not adapter._turns
    _, expired = await collect(
        adapter,
        request(
            [{"id": "result", "role": "tool", "toolCallId": "f1", "content": "late"}],
            tools=tools,
        ),
    )
    assert expired[-1]["type"] == "RUN_ERROR"
    assert len(provider.requests) == 1


async def test_restart_does_not_reconstruct_pending_native_stream(setup):
    provider, make = setup
    adapter = make()
    tools = [{"name": "browser", "description": "", "parameters": {}}]
    provider.reply(calls=[("f1", "browser", "{}")])
    view, _ = await collect(adapter, request(tools=tools))
    await adapter.close()
    replacement = make()
    _, failed = await collect(
        replacement,
        request(
            [
                *(await view.history())["messages"],
                {"id": "result", "role": "tool", "toolCallId": "f1", "content": "late"},
            ],
            tools=tools,
        ),
    )
    assert failed[-1]["type"] == "RUN_ERROR"
    assert "cannot survive" in failed[-1]["message"]
    assert len(provider.requests) == 1


async def test_pause_waits_for_sibling_backend_and_preserves_native_batch_order(setup):
    import asyncio

    provider, make = setup
    started, release = asyncio.Event(), asyncio.Event()

    async def backend():
        started.set()
        await release.wait()
        return "server"

    adapter = make([Tool.from_callable(backend)])
    tools = [{"name": "browser", "description": "", "parameters": {}}]
    provider.reply(calls=[("f1", "browser", "{}"), ("b1", "backend", "{}")])
    running = asyncio.create_task(collect(adapter, request(tools=tools)))
    await asyncio.wait_for(started.wait(), 1)
    live = adapter._turns[adapter.session_id("alice", "t1")]
    assert live.inbound.external.pending
    assert not live.inbound.external.paused.is_set()
    assert not running.done()
    release.set()
    _, events = await asyncio.wait_for(running, 1)
    assert events[-1]["type"] == "RUN_FINISHED"
    provider.reply("done")
    _, events = await collect(
        adapter,
        request(
            [{"id": "result", "role": "tool", "toolCallId": "f1", "content": "client"}],
            tools=tools,
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [
        m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "tool"
    ] == ["client", "server"]


async def test_native_merge_shield_does_not_disable_output_hook_timeout(setup):
    import asyncio

    provider, make = setup
    adapter = make(max_run_seconds=0.05)

    class Output:
        @hookimpl
        async def save_state(self):
            await asyncio.Event().wait()

    adapter.framework.plugin_manager.register(Output(), "blocked-save")
    provider.reply("finished model")
    _, events = await asyncio.wait_for(collect(adapter, request()), 1)
    assert events[-1]["type"] == "RUN_ERROR"
    assert "timed out" in events[-1]["message"]
    assert not adapter._turns
