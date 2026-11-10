"""The pinned API needs a step bridge, not a second copy of Bub's policies."""

import httpx2
from bub import hookimpl
from bub.core.events import MessagesPending
from bub.core.tape import session_tape_name
from bub.core.tools import Tool

from .conftest import collect, request


async def test_each_step_uses_native_tool_preparation_and_continue_hook(setup):
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
    assert "later" in {t["function"]["name"] for t in provider.requests[-1]["tools"]}
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


async def test_context_overflow_does_not_trigger_adapter_handoff_or_retry(setup):
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
    _, events = await collect(adapter, request())
    assert len(provider.requests) == 1
    assert events[-1]["type"] == "RUN_ERROR"
    tape = adapter._storage_agent.tape(
        session_tape_name(adapter.session_id("alice", "t1"), adapter.workspace)
    )
    entries = list(await tape.query().all())
    assert [e.payload["name"] for e in entries if e.kind == "anchor"] == [
        "session/start"
    ]
    assert not any(
        e.payload.get("name") in {"loop.start", "loop.finish"} for e in entries
    )


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
