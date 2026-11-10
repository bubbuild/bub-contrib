"""Bub owns one append-only thread tape, including context and settings."""

from itertools import pairwise

import pytest
from bub.builtin.settings import (
    AgentSettings,
    load_session_settings,
    set_session_setting,
)
from bub.builtin.store import FuzzyFileTapeStore
from bub.core.store import InMemoryTapeStore
from bub.core.tape import TapeQuery, session_tape_name
from bub.core.tools import Tool
from bub_tapestore_sqlite import SQLiteTapeStore

from .conftest import collect, request


@pytest.fixture(params=["memory", "file", "sqlite"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        value = SQLiteTapeStore(tmp_path / "tapes.sqlite3")
    elif request.param == "file":
        value = FuzzyFileTapeStore(tmp_path / "tapes")
    else:
        value = InMemoryTapeStore()
    yield value
    if isinstance(value, SQLiteTapeStore):
        await value.close()


def session_tape(adapter):
    return adapter._storage_agent.tape(
        session_tape_name(adapter.session_id("alice", "t1"), adapter.workspace)
    )


async def test_native_setting_switches_persist_and_clear_on_same_thread(
    setup, store, monkeypatch
):
    provider, make = setup
    observed = []
    model = AgentSettings.chat_model

    def chat_model(self, model_name=None, *, reasoning_effort=None):
        observed.append((model_name, reasoning_effort))
        return model(self, model_name, reasoning_effort=reasoning_effort)

    monkeypatch.setattr(AgentSettings, "chat_model", chat_model)

    async def switch(context, value):
        await set_session_setting(context, "reasoning_effort", value)
        return "updated"

    tool = Tool.from_callable(switch, context=True)
    adapter = make([tool], tape_store=store)
    provider.reply(calls=[("s1", "switch", '{"value":"high"}')])
    provider.reply("first answer")
    view, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert await load_session_settings(session_tape(adapter)) == {
        "reasoning_effort": "high"
    }

    # A fresh adapter must restore from native storage, not an in-memory cache.
    adapter = make([tool], tape_store=store)
    observed.clear()
    provider.reply(calls=[("s2", "switch", '{"value":null}')])
    provider.reply("cleared")
    view, events = await collect(
        adapter,
        request(
            [
                *(await view.history())["messages"],
                {"id": "u2", "role": "user", "content": "Clear setting"},
            ],
            run="r2",
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert observed[0] == (None, "high")
    assert await load_session_settings(session_tape(adapter)) == {}

    observed.clear()
    provider.reply("third answer")
    await collect(
        adapter,
        request(
            [
                *(await view.history())["messages"],
                {"id": "u3", "role": "user", "content": "Continue"},
            ],
            run="r3",
        ),
    )
    assert observed[0] == (None, None)


async def test_one_native_tape_appends_new_input_without_copying_echoed_history(
    setup, store
):
    provider, make = setup
    adapter = make(tape_store=store)
    messages = [{"id": "u1", "role": "user", "content": "First"}]
    sizes = []
    previous = []
    for turn in range(6):
        provider.reply(f"answer {turn}")
        view, events = await collect(adapter, request(messages, run=f"r{turn}"))
        assert events[-1]["type"] == "RUN_FINISHED"
        tape = session_tape(adapter)
        names = await tape.store.list_tapes()
        assert names == [tape.name]
        entries = list(await tape.query().all())
        assert entries[: len(previous)] == previous
        previous = entries
        sizes.append(len(entries))
        messages = [
            *(await view.history())["messages"],
            {"id": f"u{turn + 2}", "role": "user", "content": f"question {turn + 1}"},
        ]
    # Native loop records grow linearly too, but earlier input is never copied.
    increments = [b - a for a, b in pairwise(sizes)]
    assert len(set(increments)) == 1 and increments[0] > 0
    entries = list(await tape.query().kinds("message").all())
    assert sum(e.payload.get("role") == "user" for e in entries) == 6
    assert sum(e.payload.get("role") == "assistant" for e in entries) == 6
    provider.reply("next answer")
    _, events = await collect(
        adapter,
        request(
            [{"id": "next", "role": "user", "content": "Just new input"}], run="next"
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    assert len(list(await tape.query().all())) == sizes[-1] + increments[0]
    assert provider.requests[-1]["messages"][-1]["content"] == "Just new input"
    assert any(m.get("content") == "First" for m in provider.requests[-1]["messages"])


async def test_sqlite_reopen_retains_settings_without_orphan_run_tapes(setup, tmp_path):
    provider, make = setup
    path = tmp_path / "reopen.sqlite3"
    store = SQLiteTapeStore(path)

    async def switch(context):
        await set_session_setting(context, "code_mode", False)
        return "set"

    adapter = make([Tool.from_callable(switch, context=True)], tape_store=store)
    provider.reply(calls=[("s", "switch", "{}")])
    provider.reply("done")
    await collect(adapter, request())
    name = session_tape(adapter).name
    await adapter.close()
    await store.close()
    store = SQLiteTapeStore(path)
    try:
        adapter = make(tape_store=store)
        assert await store.list_tapes() == [name]
        assert await load_session_settings(session_tape(adapter)) == {
            "code_mode": False
        }
        provider.reply("after reopen")
        _, events = await collect(
            adapter,
            request(
                [{"id": "after", "role": "user", "content": "After reopen"}],
                run="after-reopen",
            ),
        )
        assert events[-1]["type"] == "RUN_FINISHED"
        assert await load_session_settings(session_tape(adapter)) == {
            "code_mode": False
        }
    finally:
        await store.close()


async def test_native_deferred_tools_and_sidecars_survive_next_turn(setup, store):
    from bub.builtin.tools import TOOL_LOADED_EVENT, loaded_tool_names
    from bub.core.tape import TapeEntry

    provider, make = setup
    adapter = make(
        [Tool.from_callable(lambda: "loaded", name="lookup", deferred=True)],
        tape_store=store,
    )
    tape = session_tape(adapter)
    await tape.append_event(TOOL_LOADED_EVENT, {"names": ["lookup"]})
    sidecar_name = tape.sidecar_tape_name("spill")
    await tape.store.append(
        sidecar_name, TapeEntry.event("test.spill", {"handle": "retained"})
    )
    provider.reply("first")
    view, _ = await collect(adapter, request())
    provider.reply("second")
    await collect(
        adapter,
        request(
            [
                *(await view.history())["messages"],
                {"id": "u2", "role": "user", "content": "Continue"},
            ],
            run="r2",
        ),
    )
    for model_request in provider.requests:
        assert "lookup" in {t["function"]["name"] for t in model_request["tools"]}
    assert await loaded_tool_names(tape) == ["lookup"]
    entries = list(
        await tape.store.fetch_all(TapeQuery(tape=sidecar_name, store=tape.store))
    )
    assert len(entries) == 1
    assert entries[0].payload["data"]["handle"] == "retained"


async def test_cancelled_receipt_append_settles_before_releasing_thread(
    setup, store, monkeypatch
):
    import asyncio

    provider, make = setup
    adapter = make(tape_store=store)
    tape = session_tape(adapter)
    from bub.core.tools import ToolContext

    await set_session_setting(ToolContext(tape=tape), "reasoning_effort", "high")
    entered, release = asyncio.Event(), asyncio.Event()
    append = tape.store.append
    pause = True

    async def slow_append(name, entry):
        nonlocal pause
        await append(name, entry)
        if entry.payload.get("name") == "ag_ui.inputs" and pause:
            pause = False
            entered.set()
            await release.wait()

    provider.reply("recorded natively")
    monkeypatch.setattr(tape.store, "append", slow_append)
    task = asyncio.create_task(collect(adapter, request()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        _, busy = await collect(adapter, request(run="busy"))
        assert busy[-1]["type"] == "RUN_ERROR"
        assert "Another request" in busy[-1]["message"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert await load_session_settings(tape) == {"reasoning_effort": "high"}
    assert len(provider.requests) == 1
    assert not adapter._active
    provider.reply("next")
    _, events = await collect(
        adapter,
        request(
            [{"id": "after", "role": "user", "content": "After cancellation"}],
            run="next",
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
