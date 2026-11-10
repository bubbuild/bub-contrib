"""Conversation and session settings survive subsequent client requests."""

import json

import pytest
from bub.builtin.settings import set_session_setting
from bub.builtin.store import FuzzyFileTapeStore
from bub.core.store import InMemoryTapeStore
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


async def test_session_settings_persist_and_can_be_cleared(setup, store):
    provider, make = setup

    async def switch(context, value):
        previous = context.state.get("reasoning_effort")
        await set_session_setting(context, "reasoning_effort", value)
        return previous or "default"

    tools = [Tool.from_callable(switch, context=True)]
    for index, (value, expected) in enumerate(
        [("high", "default"), (None, "high"), (None, "default")]
    ):
        # Recreate the adapter: persistence must be visible without its memory.
        adapter = make(tools, tape_store=store)
        provider.reply(calls=[(f"s{index}", "switch", json.dumps({"value": value}))])
        provider.reply("done")
        _, events = await collect(
            adapter,
            request(
                [{"id": f"u{index}", "role": "user", "content": "Update setting"}],
                run=f"r{index}",
            ),
        )
        assert events[-1]["type"] == "RUN_FINISHED"
        assert [e["content"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == [
            expected
        ]
        await adapter.close()


async def test_echoed_history_does_not_duplicate_model_context(setup, store):
    provider, make = setup
    adapter = make(tape_store=store)
    messages = [{"id": "u0", "role": "user", "content": "First"}]
    expected = ["First"]
    for turn in range(3):
        provider.reply(f"answer {turn}")
        view, events = await collect(adapter, request(messages, run=f"r{turn}"))
        assert events[-1]["type"] == "RUN_FINISHED"
        assert [
            m["content"]
            for m in provider.requests[-1]["messages"]
            if m["role"] != "system"
        ] == expected
        question = f"question {turn + 1}"
        expected.extend([f"answer {turn}", question])
        messages = [
            *(await view.history())["messages"],
            {"id": f"u{turn + 1}", "role": "user", "content": question},
        ]


async def test_sqlite_reopen_retains_conversation(setup, tmp_path):
    provider, make = setup
    path = tmp_path / "reopen.sqlite3"
    store = SQLiteTapeStore(path)
    adapter = make(tape_store=store)
    try:
        provider.reply("remember this answer")
        await collect(adapter, request())
    finally:
        await adapter.close()
        await store.close()
    store = SQLiteTapeStore(path)
    adapter = make(tape_store=store)
    try:
        provider.reply("after reopen")
        _, events = await collect(
            adapter,
            request(
                [{"id": "after", "role": "user", "content": "Continue"}],
                run="after",
            ),
        )
        assert events[-1]["type"] == "RUN_FINISHED"
        assert [
            m["content"]
            for m in provider.requests[-1]["messages"]
            if m["role"] != "system"
        ] == ["Hello", "remember this answer", "Continue"]
    finally:
        await adapter.close()
        await store.close()
