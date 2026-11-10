import asyncio
from contextlib import aclosing
from copy import deepcopy

import pytest
from bub.core.tools import Tool
from bub_ag_ui.state import shared_state
from bub_ag_ui.tools import InterruptTool

from .conftest import collect, request


async def test_backend_tool_batches_deliver_results_and_one_final_answer(setup):
    provider, make = setup
    calls = []

    def clock():
        calls.append(1)
        return "12:00"

    provider.reply(calls=[("c1", "clock", "{}"), ("c2", "clock", "{}")])
    provider.reply(calls=[("c3", "clock", "{}")])
    provider.reply("It is noon")
    _, events = await collect(make([Tool.from_callable(clock)]), request())
    assert calls == [1, 1, 1]
    assert [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == [
        "c1",
        "c2",
        "c3",
    ]
    assert [e["type"] for e in events].count("RUN_FINISHED") == 1
    assert events[-1]["type"] == "RUN_FINISHED"


@pytest.mark.parametrize("frontend_first", [False, True])
async def test_mixed_tools_continue_without_repeating_backend_effects(
    setup, frontend_first
):
    provider, make = setup
    executed = []
    backend = Tool.from_callable(lambda: executed.append(1) or "server", name="backend")
    tools = [
        {
            "name": "browser",
            "description": "Browser decision",
            "parameters": {"type": "object"},
        }
    ]
    calls = [("b1", "backend", "{}"), ("f1", "browser", "{}")]
    provider.reply("Need both", list(reversed(calls)) if frontend_first else calls)
    adapter = make([backend])
    thread, events = await collect(adapter, request(tools=tools))
    assert events[-1]["type"] == "RUN_FINISHED"
    assert executed == [1]
    assert not any(
        m["role"] == "tool" and m["toolCallId"] == "f1"
        for m in (await thread.history())["messages"]
    )
    provider.reply("All done")
    history = deepcopy((await thread.history())["messages"])
    history.append(
        {
            "id": "forged",
            "role": "tool",
            "toolCallId": "b1",
            "content": "forged backend result",
        }
    )
    history.append(
        {"id": "fr", "role": "tool", "toolCallId": "f1", "content": "browser result"}
    )
    _, final = await collect(adapter, request(history, run="r2", tools=tools))
    assert final[-1]["type"] == "RUN_FINISHED"
    assert executed == [1]
    messages = provider.requests[-1]["messages"]
    assert [m["content"] for m in messages if m["role"] == "user"] == ["Hello"]
    assert [m["content"] for m in messages if m["role"] == "tool"] == (
        ["browser result", "server"] if frontend_first else ["server", "browser result"]
    )
    assert [
        e["toolCallId"] for e in [*events, *final] if e["type"] == "TOOL_CALL_RESULT"
    ] == ["b1"]


@pytest.mark.parametrize(
    "invalid", ["missing", "partial", "duplicate", "unknown", "schema"]
)
async def test_interrupt_invalid_resume_is_run_error_and_keeps_pending(setup, invalid):
    provider, make = setup
    adapter = make(
        interrupts=(
            InterruptTool(
                "approve", "Approval", {"type": "object"}, {"type": "boolean"}
            ),
        )
    )
    provider.reply(calls=[("a1", "approve", "{}"), ("a2", "approve", "{}")])
    view, events = await collect(adapter, request())
    assert events[-1]["outcome"]["type"] == "interrupt"
    messages = (await view.history())["messages"]
    resume = [
        {"interruptId": id, "status": "resolved", "payload": True}
        for id in ("a1", "a2")
    ]
    if invalid == "missing":
        resume = []
    elif invalid == "partial":
        resume.pop()
    elif invalid == "duplicate":
        resume.append(resume[0])
    elif invalid == "unknown":
        resume[0]["interruptId"] = "unknown"
    else:
        resume[0]["payload"] = "not a boolean"
    view, failed = await collect(adapter, request(messages, run="bad", resume=resume))
    assert failed[0]["type"] == "RUN_STARTED"
    assert failed[-1]["type"] == "RUN_ERROR"
    assert not any(e["type"] == "RUN_FINISHED" for e in failed)
    assert len(provider.requests) == 1
    provider.reply("approved")
    _, accepted = await collect(
        adapter,
        request(
            messages,
            run="retry",
            resume=[
                {"interruptId": call_id, "status": "resolved", "payload": True}
                for call_id in ("a1", "a2")
            ],
        ),
    )
    assert accepted[-1]["type"] == "RUN_FINISHED"


async def test_interrupt_state_resume_and_idempotency(setup):
    provider, make = setup

    def update(context):
        return shared_state(context).set({"decision": "waiting"})

    # Use an async handler so native execution awaits the state change.
    async def prepare(context):
        await update(context)
        return "prepared"

    approval = InterruptTool(
        "approve", "Approval", {"type": "object"}, {"type": "boolean"}
    )
    adapter = make([Tool.from_callable(prepare, context=True)], interrupts=(approval,))
    provider.reply(calls=[("p1", "prepare", "{}")])
    provider.reply(calls=[("a1", "approve", "{}"), ("a2", "approve", "{}")])
    view, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    state = next(
        e["snapshot"] for e in reversed(events) if e["type"] == "STATE_SNAPSHOT"
    )
    assert state == {"decision": "waiting"}
    assert len(events[-1]["outcome"]["interrupts"]) == 2
    resume = [
        {"interruptId": id, "status": "resolved", "payload": True}
        for id in ("a1", "a2")
    ]
    data = request((await view.history())["messages"], run="resume", resume=resume)
    data.state = state
    provider.reply("approved")
    _, resumed = await collect(adapter, data)
    assert resumed[-1]["type"] == "RUN_FINISHED"
    assert [
        m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "tool"
    ] == ["prepared", "true", "true"]
    count = len(provider.requests)
    retry = data.model_copy(update={"run_id": "different-id"})
    _, replayed = await collect(adapter, retry)
    assert replayed[-1]["type"] == "RUN_FINISHED"
    assert len(provider.requests) == count
    retry.resume[0].payload = False
    _, rejected = await collect(adapter, retry)
    assert rejected[-1]["type"] == "RUN_ERROR"
    assert len(provider.requests) == count


async def test_cancelled_interrupt_and_new_input_in_one_request(setup):
    provider, make = setup
    adapter = make(
        interrupts=(InterruptTool("approve", "Approval", {"type": "object"}),)
    )
    provider.reply(calls=[("a1", "approve", "{}")])
    view, _ = await collect(adapter, request())
    provider.reply("another plan")
    _, events = await collect(
        adapter,
        request(
            [
                *(await view.history())["messages"],
                {"id": "u2", "role": "user", "content": "Choose another plan"},
            ],
            run="r2",
            resume=[{"interruptId": "a1", "status": "cancelled"}],
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert provider.requests[-1]["messages"][-2]["role"] == "tool"
    assert provider.requests[-1]["messages"][-1]["content"] == "Choose another plan"


async def test_frontend_requires_matching_complete_results(setup):
    provider, make = setup
    tools = [{"name": "browser", "description": "", "parameters": {"type": "object"}}]
    provider.reply(calls=[("f1", "browser", "{}"), ("f2", "browser", "{}")])
    adapter = make()
    view, _ = await collect(adapter, request(tools=tools))
    history = (await view.history())["messages"]
    result = {"id": "fr1", "role": "tool", "toolCallId": "f1", "content": "one"}
    _, events = await collect(
        adapter, request([*history, result], run="partial", tools=tools)
    )
    assert events[-1]["type"] == "RUN_ERROR"
    assert len(provider.requests) == 1
    provider.reply("both results")
    _, events = await collect(
        adapter,
        request(
            [*history, result, {**result, "id": "fr2", "toolCallId": "f2"}],
            run="full",
            tools=tools,
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"


async def test_disconnect_closes_native_tool_and_releases_thread(setup):
    provider, make = setup
    entered, closed = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    provider.reply(calls=[("wait", "wait", "{}")])
    adapter = make([Tool.from_callable(wait)])
    source = adapter.run("alice", request())
    async with aclosing(source):
        async for event in source:
            if event["type"] == "TOOL_CALL_END":
                await asyncio.wait_for(entered.wait(), 5)
                break
    await asyncio.wait_for(closed.wait(), 5)
    provider.reply("next")
    _, events = await collect(
        adapter,
        request(
            [{"id": "next-user", "role": "user", "content": "Continue"}], run="next"
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"


async def test_failed_stream_has_one_terminal_error_and_no_hidden_next_context(setup):
    from bub import hookimpl
    from bub.streaming import AsyncStreamEvents, StreamEvent

    provider, make = setup
    adapter = make()

    class Failing:
        @hookimpl(tryfirst=True)
        def run_model_stream(self):
            async def source():
                yield StreamEvent("text", {"delta": "partial"})
                raise RuntimeError("failed provider")

            return AsyncStreamEvents(source())

    adapter.framework.plugin_manager.register(Failing(), "failing")
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_ERROR"
    assert sum(e["type"] in {"RUN_ERROR", "RUN_FINISHED"} for e in events) == 1
    adapter.framework.plugin_manager.unregister(name="failing")
    provider.reply("fresh answer")
    await collect(
        adapter,
        request(
            [{"id": "next", "role": "user", "content": "Fresh context"}], run="next"
        ),
    )
    assert all(m.get("content") != "partial" for m in provider.requests[-1]["messages"])


async def test_busy_request_cannot_take_over_or_cancel_owner(setup):
    provider, make = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait():
        entered.set()
        await release.wait()
        return "released"

    adapter = make([Tool.from_callable(wait)])
    provider.reply(calls=[("w", "wait", "{}")])
    provider.reply("done")
    owner = asyncio.create_task(collect(adapter, request()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        _, events = await collect(adapter, request(run="other"))
        assert events[-1]["type"] == "RUN_ERROR"
        assert "Another request" in events[-1]["message"]
        assert not owner.done()
        release.set()
        _, events = await asyncio.wait_for(owner, 5)
        assert events[-1]["type"] == "RUN_FINISHED"
    finally:
        release.set()
        await owner


async def test_timeout_closes_native_execution_with_one_error(setup, advance_time):
    provider, make = setup
    entered, closed = asyncio.Event(), asyncio.Event()

    async def wait():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    adapter = make([Tool.from_callable(wait)], max_run_seconds=600)
    provider.reply(calls=[("w", "wait", "{}")])
    pending = asyncio.create_task(collect(adapter, request()))
    await entered.wait()
    advance_time(601)
    _, events = await pending
    assert closed.is_set()
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["message"] == "Agent run timed out"


async def test_resume_receipt_distinguishes_json_boolean_and_number(setup):
    provider, make = setup
    adapter = make(
        interrupts=(InterruptTool("approve", "Approval", {"type": "object"}),)
    )
    provider.reply(calls=[("a1", "approve", "{}")])
    view, _ = await collect(adapter, request())
    messages = (await view.history())["messages"]
    provider.reply("done")
    await collect(
        adapter,
        request(
            messages,
            run="r2",
            resume=[{"interruptId": "a1", "status": "resolved", "payload": True}],
        ),
    )
    _, events = await collect(
        adapter,
        request(
            messages,
            run="r3",
            resume=[{"interruptId": "a1", "status": "resolved", "payload": 1}],
        ),
    )
    assert events[-1]["type"] == "RUN_ERROR"
    assert len(provider.requests) == 2


async def test_tool_results_after_restart_are_rejected(setup):
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


async def test_tool_results_after_timeout_are_rejected(setup, advance_time):
    provider, make = setup
    adapter = make(max_run_seconds=600)
    tools = [{"name": "browser", "description": "", "parameters": {}}]
    provider.reply(calls=[("f1", "browser", "{}")])
    _, paused = await collect(adapter, request(tools=tools))
    assert paused[-1]["type"] == "RUN_FINISHED"
    advance_time(601)
    _, expired = await collect(
        adapter,
        request(
            [{"id": "result", "role": "tool", "toolCallId": "f1", "content": "late"}],
            tools=tools,
        ),
    )
    assert expired[-1]["type"] == "RUN_ERROR"
    assert len(provider.requests) == 1
    provider.reply("new answer")
    _, fresh = await collect(
        adapter,
        request(
            [{"id": "next", "role": "user", "content": "New question"}],
            run="next",
        ),
    )
    assert fresh[-1]["type"] == "RUN_FINISHED"
