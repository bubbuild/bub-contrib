import asyncio
from copy import deepcopy

import pytest
from bub.core.tools import Tool
from bub_ag_ui.runtime import ProtocolError
from bub_ag_ui.state import shared_state
from bub_ag_ui.tools import InterruptTool

from .conftest import collect, request


async def test_text_history_handoff_and_retry(setup):
    provider, make = setup
    provider.reply("Hello world")
    adapter = make()
    data = request()
    thread, events = await collect(adapter, data)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"] == [
        "Hello world"
    ]
    assert len([e for e in events if e["type"] == "RUN_FINISHED"]) == 1
    _, replay = await collect(adapter, data)
    assert events == replay
    assert len(provider.requests) == 1
    first_messages = deepcopy((await thread.history())["messages"])
    provider.reply("second")
    next_messages = first_messages + [{"id": "u2", "role": "user", "content": "Again"}]
    await collect(adapter, request(next_messages, run="r2"))
    assert [
        m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "user"
    ] == ["Hello", "Again"]
    await thread.tape.handoff(name="summary", state={"summary": "short"})
    assert (await thread.history())["messages"][: len(first_messages)] == first_messages
    assert await make().history("alice", "t1") == await adapter.history("alice", "t1")


async def test_backend_tool_multiple_steps_and_errors(setup):
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
    assert [e["type"] for e in events].count("STEP_STARTED") == 3
    assert events[-1]["type"] == "RUN_FINISHED"


@pytest.mark.parametrize("frontend_first", [False, True])
async def test_frontend_mixed_tools_resume_across_restart(setup, frontend_first):
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
    assert [c["id"] for c in (await thread.history())["pending"]] == ["f1"]
    assert executed == [1]
    assert [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == [
        "b1"
    ]
    assert not any(
        m["role"] == "tool" and m["toolCallId"] == "f1"
        for m in (await thread.history())["messages"]
    )
    provider.reply("All done")
    history = deepcopy((await thread.history())["messages"])
    # An echoed result is not a request to overwrite the server's stored result.
    next(m for m in history if m["role"] == "tool")["content"] = "changed echo"
    history.append(
        {"id": "fr", "role": "tool", "toolCallId": "f1", "content": "browser result"}
    )
    restored = make([backend])
    thread2, final = await collect(restored, request(history, run="r2", tools=tools))
    assert final[-1]["type"] == "RUN_FINISHED"
    assert (await thread2.history())["pending"] == []
    assert executed == [1]
    messages = provider.requests[-1]["messages"]
    assert [m["content"] for m in messages if m["role"] == "user"] == ["Hello"]
    assert [m["content"] for m in messages if m["role"] == "tool"] == [
        "server",
        "browser result",
    ]


async def test_partial_frontend_results_do_not_run_model(setup):
    provider, make = setup
    tools = [{"name": "browser", "description": "", "parameters": {"type": "object"}}]
    provider.reply(calls=[("f1", "browser", "{}"), ("f2", "browser", "{}")])
    adapter = make()
    thread, _ = await collect(adapter, request(tools=tools))
    result = {"id": "fr1", "role": "tool", "toolCallId": "f1", "content": "one"}
    thread, events = await collect(
        adapter,
        request([*(await thread.history())["messages"], result], run="r2", tools=tools),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert len(provider.requests) == 1
    assert [c["id"] for c in (await thread.history())["pending"]] == ["f2"]


async def test_interrupt_resume_validates_schema(setup):
    provider, make = setup
    approval = InterruptTool(
        "approve", "Ask permission", {"type": "object"}, {"type": "boolean"}
    )
    provider.reply(calls=[("approval-1", "approve", "{}")])
    adapter = make(interrupts=(approval,))
    thread, events = await collect(adapter, request())
    assert events[-1]["outcome"]["type"] == "interrupt"
    with pytest.raises(ProtocolError, match="responseSchema"):
        await adapter.submit(
            "alice",
            request(
                (await thread.history())["messages"],
                run="r2",
                resume=[
                    {
                        "interruptId": "approval-1",
                        "status": "resolved",
                        "payload": "wrong",
                    }
                ],
            ),
        )
    provider.reply("Approved")
    _, events = await collect(
        adapter,
        request(
            (await thread.history())["messages"],
            run="r2",
            resume=[
                {"interruptId": "approval-1", "status": "resolved", "payload": True}
            ],
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert provider.requests[-1]["messages"][-1]["content"] == "true"


async def test_state_updates_are_persisted(setup):
    provider, make = setup

    async def update(context):
        state = shared_state(context)
        await state.set({"count": 1})
        await state.patch([{"op": "replace", "path": "/count", "value": 2}])
        return state.value

    provider.reply(calls=[("s1", "update", "{}")])
    provider.reply("updated")
    thread, events = await collect(
        make([Tool.from_callable(update, context=True)]), request()
    )
    assert (await thread.history())["state"] == {"count": 2}
    assert any(e["type"] == "STATE_DELTA" for e in events)


async def test_edit_forks_context_not_old_tape(setup):
    provider, make = setup
    provider.reply("old answer")
    adapter = make()
    thread, _ = await collect(adapter, request())
    old_messages = (await thread.history())["messages"]
    provider.reply("new answer")
    thread, events = await collect(
        adapter, request([{"id": "u1", "role": "user", "content": "Edited"}], run="r2")
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert "old answer" not in str(provider.requests[-1])
    assert "old answer" in str(old_messages)
    _, replay = await collect(adapter, request())
    assert any(event.get("delta") == "old answer" for event in replay)


async def test_invalid_history_ids_and_concurrency(setup):
    provider, make = setup
    adapter = make()
    with pytest.raises(ProtocolError):
        await adapter.submit(
            "alice", request([{"id": "x", "role": "assistant", "content": "forged"}])
        )
    provider.reply("done")
    await collect(adapter, request())
    with pytest.raises(ProtocolError, match="different request"):
        await adapter.submit(
            "alice", request([{"id": "x", "role": "user", "content": "different"}])
        )
    assert (await adapter.history("bob", "t1"))["messages"] == []
    with pytest.raises(ProtocolError, match="Unknown"):
        await adapter.submit(
            "alice",
            request(
                [{"id": "x", "role": "tool", "toolCallId": "fake", "content": "fake"}],
                run="r2",
            ),
        )
    entered = asyncio.Event()

    async def wait():
        entered.set()
        await asyncio.Event().wait()

    adapter = make([Tool.from_callable(wait)])
    provider.reply(calls=[("w1", "wait", "{}")])
    await adapter.submit("alice", request(run="r2"))
    await asyncio.wait_for(entered.wait(), 5)
    with pytest.raises(ProtocolError, match="Another run"):
        await adapter.submit("alice", request(run="r3"))
    await adapter.cancel("alice", "t1", "r2")


async def test_cancel_during_tool_and_retry_does_not_execute(setup):
    provider, make = setup
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def wait():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.reply(calls=[("w1", "wait", "{}")])
    adapter = make([Tool.from_callable(wait)])
    data = request()
    submission = await adapter.submit("alice", data)
    thread = submission.thread
    await asyncio.wait_for(entered.wait(), 5)
    assert await adapter.cancel("alice", "t1", "r1")
    assert cancelled.is_set()
    events = [r["event"] async for r in adapter.events(thread, "r1")]
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "CANCELLED"
    _, replay = await collect(adapter, data)
    assert replay == events
    assert len(provider.requests) == 1


async def test_failure_never_emits_success(setup):
    provider, make = setup
    adapter = make()
    # Empty fake response queue raises in the model transport.
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_ERROR"
    assert not any(e["type"] == "RUN_FINISHED" for e in events)
    provider.reply("recovered")
    _, next_events = await collect(adapter, request(run="r2"))
    assert next_events[-1]["type"] == "RUN_FINISHED"


async def test_restart_marks_unfinished_run_failed_without_executing(setup):
    from bub_ag_ui.events import wire_event
    from bub_ag_ui.runtime import fingerprint

    provider, make = setup
    adapter = make()
    provider.reply("recorded answer")
    thread, _ = await collect(adapter, request(run="completed"))
    data = request()
    await thread.append("admit", runId="r1", fingerprint=fingerprint(data))
    await thread.append(
        "event", runId="r1", event=wire_event("RUN_STARTED", threadId="t1", runId="r1")
    )
    await thread.append(
        "event",
        runId="r1",
        event=wire_event("TEXT_MESSAGE_START", messageId="partial", role="assistant"),
    )
    await thread.append(
        "event",
        runId="r1",
        event=wire_event("TEXT_MESSAGE_CONTENT", messageId="partial", delta="partial"),
    )
    restored = make()
    recovered, events = await collect(restored, data)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "INTERRUPTED"
    assert events[-2]["type"] == "TEXT_MESSAGE_END"
    assert (await recovered.history())["messages"][-1]["content"] == "partial"
    _, replay = await collect(make(), data)
    assert replay == events
    assert len(provider.requests) == 1
    provider.reply("next answer")
    await collect(
        restored,
        request(
            [
                *(await recovered.history())["messages"],
                {"id": "u2", "role": "user", "content": "Continue"},
            ],
            run="r2",
        ),
    )
    # Restart retains provider-owned history, not its display-only partial output.
    assert [
        m["content"]
        for m in provider.requests[-1]["messages"]
        if m["role"] == "assistant"
    ] == ["recorded answer"]


async def test_recoverable_backend_error_and_command_text(setup):
    provider, make = setup

    def fail():
        raise ValueError("tool failed")

    provider.reply(calls=[("c1", "fail", "{}")])
    provider.reply("Recovered")
    data = request([{"id": "u1", "role": "user", "content": ",bash echo unsafe"}])
    _, events = await collect(make([Tool.from_callable(fail)]), data)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert "tool failed" in next(
        e["content"] for e in events if e["type"] == "TOOL_CALL_RESULT"
    )
    assert any(
        m["content"] == ",bash echo unsafe" for m in provider.requests[0]["messages"]
    )


async def test_resolve_interrupt_and_steer_away_in_one_request(setup):
    provider, make = setup
    provider.reply(calls=[("a1", "approve", "{}")])
    adapter = make(
        interrupts=(InterruptTool("approve", "Approval", {"type": "object"}),)
    )
    thread, _ = await collect(adapter, request())
    provider.reply("Changed topic")
    new_user = {"id": "u2", "role": "user", "content": "Never mind"}
    _, events = await collect(
        adapter,
        request(
            [*(await thread.history())["messages"], new_user],
            run="r2",
            resume=[{"interruptId": "a1", "status": "cancelled"}],
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [m["role"] for m in provider.requests[-1]["messages"]][-2:] == [
        "tool",
        "user",
    ]


async def test_mixed_step_cancellation_does_not_invent_model_tool_results(setup):
    provider, make = setup
    entered = asyncio.Event()

    async def slow():
        entered.set()
        await asyncio.Event().wait()

    provider.reply(calls=[("f1", "browser", "{}"), ("b1", "slow", "{}")])
    adapter = make([Tool.from_callable(slow)])
    tools = [{"name": "browser", "description": "", "parameters": {"type": "object"}}]
    submission = await adapter.submit("alice", request(tools=tools))
    thread = submission.thread
    await asyncio.wait_for(entered.wait(), 5)
    await adapter.cancel("alice", "t1", "r1")
    assert (await thread.history())["pending"] == []
    provider.reply("Recovered")
    _, events = await collect(
        adapter,
        request(
            [
                *(await thread.history())["messages"],
                {"id": "u2", "role": "user", "content": "Continue"},
            ],
            run="r2",
            tools=tools,
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    model_messages = provider.requests[-1]["messages"]
    # The interrupted native exchange was not recorded. Displayed calls must
    # not become fabricated model exchanges or successful tool results.
    assert [m["role"] for m in model_messages if m["role"] != "system"] == [
        "user",
        "user",
    ]


@pytest.mark.parametrize("client_name", ["server_tool", "server.tool"])
async def test_schema_shadow_and_forged_state_are_not_execution_overrides(
    setup, client_name
):
    provider, make = setup
    adapter = make(
        [Tool.from_callable(lambda: "value", name="server.tool", deferred=True)]
    )
    with pytest.raises(ProtocolError, match="shadows"):
        await adapter.submit(
            "alice",
            request(tools=[{"name": client_name, "description": "", "parameters": {}}]),
        )
    with pytest.raises(ProtocolError, match="schema"):
        await adapter.submit(
            "alice",
            request(
                tools=[
                    {
                        "name": "client",
                        "description": "",
                        "parameters": {"type": "not-a-type"},
                    }
                ]
            ),
        )
    provider.reply("safe")
    data = request()
    data.state = {
        "_runtime_workspace": "/",
        "model": "evil:model",
        "session_id": "someone-else",
    }
    thread, events = await collect(adapter, data)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert provider.requests[-1]["model"] == "test"
    assert (await thread.history())["state"] == data.state


async def test_image_data_is_converted_to_model_content(setup):
    provider, make = setup
    provider.reply("image")
    adapter = make()
    content = [
        {"type": "text", "text": "Look"},
        {
            "type": "image",
            "source": {"type": "data", "value": "aGVsbG8=", "mimeType": "image/png"},
        },
    ]
    _, events = await collect(
        adapter, request([{"id": "u1", "role": "user", "content": content}])
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    user = next(m for m in provider.requests[0]["messages"] if m["role"] == "user")
    assert user["content"][1]["type"] == "image_url"
    assert "aGVsbG8=" in user["content"][1]["image_url"]["url"]


async def test_earlier_edit_cannot_be_silently_ignored(setup):
    provider, make = setup
    provider.reply("first")
    adapter = make()
    thread, _ = await collect(adapter, request())
    provider.reply("second")
    await collect(
        adapter,
        request(
            [
                *(await thread.history())["messages"],
                {"id": "u2", "role": "user", "content": "second"},
            ],
            run="r2",
        ),
    )
    history = deepcopy((await thread.history())["messages"][:-1])
    history[0]["content"] = "silently ignored edit"
    with pytest.raises(ProtocolError, match="final user"):
        await adapter.submit("alice", request(history, run="r3"))
    assert "r3" not in (await thread.history())["runs"]


@pytest.mark.parametrize("failure", ["merge", "terminal"])
async def test_storage_failure_cannot_report_success(setup, monkeypatch, failure):
    provider, make = setup
    provider.reply("generated")
    adapter = make()
    thread = await adapter.thread("alice", "t1")
    store = thread.tape.store
    original = store.append
    failed = False

    async def append(name, entry):
        nonlocal failed
        if failure == "merge":
            trigger = (
                entry.kind == "message" and entry.payload.get("role") == "assistant"
            )
        else:
            trigger = (
                entry.kind == "event"
                and entry.payload.get("data", {}).get("event", {}).get("type")
                == "RUN_FINISHED"
            )
        if trigger and not failed:
            failed = True
            raise OSError("simulated storage failure")
        await original(name, entry)

    monkeypatch.setattr(store, "append", append)
    thread, events = await collect(adapter, request())
    assert failed
    assert events[-1]["type"] == "RUN_ERROR"
    assert not any(event["type"] == "RUN_FINISHED" for event in events)


async def test_cancellation_before_first_model_token_releases_thread(setup):
    provider, make = setup
    adapter = make()
    submission = await adapter.submit("alice", request())
    thread = submission.thread
    await adapter.cancel("alice", "t1", "r1")
    events = [r["event"] async for r in adapter.events(thread, "r1")]
    assert events[-1]["type"] == "RUN_ERROR"
    provider.reply("after cancellation")
    _, next_events = await collect(adapter, request(run="r2"))
    assert next_events[-1]["type"] == "RUN_FINISHED"


async def test_concurrent_retries_share_execution_and_replay_is_read_only(setup):
    provider, make = setup
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def wait():
        calls.append("executed")
        entered.set()
        await release.wait()
        return "ready"

    provider.reply(calls=[("w1", "wait", "{}")])
    provider.reply("finished once")
    adapter = make([Tool.from_callable(wait)])
    submissions = await asyncio.gather(
        *(adapter.submit("alice", request()) for _ in range(8))
    )
    await asyncio.wait_for(entered.wait(), 5)
    # Only the admitting HTTP response may cancel its producer on disconnect.
    assert sum(submission.created for submission in submissions) == 1
    with pytest.raises(ProtocolError, match="Another run"):
        await adapter.submit("alice", request(run="r2"))

    thread = submissions[0].thread
    observer = adapter.events(thread, "r1")
    first = await anext(observer)
    assert first["event"]["type"] == "RUN_STARTED"
    await observer.aclose()
    # Closing a replay subscription does not affect the live producer.
    release.set()
    records = [record async for record in adapter.events(thread, "r1")]
    assert records[-1]["event"]["type"] == "RUN_FINISHED"
    assert calls == ["executed"]
    assert len(provider.requests) == 2
    cursor = records[len(records) // 2]["sequence"]
    tail = [record async for record in adapter.events(thread, "r1", cursor)]
    assert tail == [record for record in records if record["sequence"] > cursor]
    assert len(provider.requests) == 2
    provider.reply("next run")
    _, events = await collect(adapter, request(run="r2"))
    assert events[-1]["type"] == "RUN_FINISHED"


@pytest.mark.parametrize("reset", ["owner", "sidecar", "fork"])
async def test_mounted_history_follows_tape_reset_and_accepts_fresh_runs(setup, reset):
    provider, make = setup
    provider.reply("before reset")
    adapter = make()
    thread, _ = await collect(adapter, request())
    before = await adapter.history("alice", "t1")
    assert "before reset" in str(before["messages"])
    if reset == "owner":
        await thread.tape.reset(keep_sidecars=("ag-ui",))
        assert await adapter.history("alice", "t1") == before
        await thread.tape.reset()
    elif reset == "sidecar":
        await thread.tape.reset_sidecar("ag-ui")
    else:
        async with thread.tape.fork_tape() as fork:
            await fork.reset()
            assert await adapter.history("alice", "t1") == before
    assert (await adapter.history("alice", "t1"))["messages"] == []
    with pytest.raises(ProtocolError, match="reset"):
        await anext(adapter.events(thread, "r1", before["cursor"]))
    provider.reply("after reset")
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert (await adapter.history("alice", "t1"))["messages"][-1][
        "content"
    ] == "after reset"


async def test_cancellation_during_admission_settles_write_before_next_run(
    setup, monkeypatch
):
    provider, make = setup
    adapter = make()
    thread = await adapter.thread("alice", "t1")
    store = thread.tape.store
    original = store.append
    entered = asyncio.Event()
    release = asyncio.Event()

    async def append(name, entry):
        if (
            entry.payload.get("name") == "ag_ui.admit"
            and entry.payload["data"]["runId"] == "r1"
        ):
            entered.set()
            await release.wait()
        await original(name, entry)

    monkeypatch.setattr(store, "append", append)
    admission = asyncio.create_task(adapter.submit("alice", request()))
    await asyncio.wait_for(entered.wait(), 5)
    admission.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await admission
    replay = [record["event"] async for record in adapter.events(thread, "r1")]
    assert replay[-1]["type"] == "RUN_ERROR"
    assert replay[-1]["code"] == "CANCELLED"
    assert provider.requests == []
    provider.reply("after cancelled admission")
    _, events = await collect(adapter, request(run="r2"))
    assert events[-1]["type"] == "RUN_FINISHED"


async def test_cancellation_racing_terminal_commit_has_one_outcome(setup, monkeypatch):
    provider, make = setup
    provider.reply("done")
    adapter = make()
    thread = await adapter.thread("alice", "t1")
    store = thread.tape.store
    original = store.append
    entered = asyncio.Event()
    release = asyncio.Event()

    async def append(name, entry):
        if entry.payload.get("data", {}).get("event", {}).get("type") == "RUN_FINISHED":
            entered.set()
            await release.wait()
        await original(name, entry)

    monkeypatch.setattr(store, "append", append)
    await adapter.submit("alice", request())
    await asyncio.wait_for(entered.wait(), 5)
    cancellation = asyncio.create_task(adapter.cancel("alice", "t1", "r1"))
    await asyncio.sleep(0)
    release.set()
    await cancellation
    events = [record["event"] async for record in adapter.events(thread, "r1")]
    assert [
        event["type"]
        for event in events
        if event["type"] in {"RUN_FINISHED", "RUN_ERROR"}
    ] == ["RUN_FINISHED"]
