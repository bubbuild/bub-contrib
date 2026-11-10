"""The transport supplies new input; Bub owns stored context and anchors."""

import pytest
from bub.core.tools import Tool
from bub_ag_ui.tools import InterruptTool

from .conftest import collect, request


async def test_incremental_user_input_keeps_native_conversation(setup):
    provider, make = setup
    adapter = make()
    provider.reply("Bub's own answer")
    await collect(adapter, request())
    provider.reply("next")
    _, events = await collect(
        adapter,
        request([{"id": "u2", "role": "user", "content": "Continue"}], run="r2"),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    contents = [m.get("content") for m in provider.requests[-1]["messages"]]
    assert "Hello" in contents
    assert "Bub's own answer" in contents
    assert "Continue" in contents


async def test_echoed_history_cannot_resurrect_messages_before_handoff(setup):
    provider, make = setup

    async def compact(context):
        await context.tape.handoff(name="summary", state={"summary": "Use the summary"})
        return "compacted"

    adapter = make([Tool.from_callable(compact, context=True)])
    provider.reply("old answer")
    view, _ = await collect(adapter, request())
    provider.reply(calls=[("compact", "compact", "{}")])
    provider.reply("summary ready")
    view, _ = await collect(
        adapter,
        request(
            [
                *(await view.history())["messages"],
                {"id": "compact-request", "role": "user", "content": "Compact context"},
            ],
            run="compact",
        ),
    )
    provider.reply("next")
    _, events = await collect(
        adapter,
        request(
            [
                *(await view.history())["messages"],
                {"id": "u2", "role": "user", "content": "Continue"},
            ],
            run="r2",
        ),
    )
    assert events[-1]["type"] == "RUN_FINISHED"
    contents = [m.get("content") for m in provider.requests[-1]["messages"]]
    assert "Hello" not in contents
    assert "old answer" not in contents
    assert "Continue" in contents


async def test_assistant_echo_is_not_a_model_context_write(setup):
    provider, make = setup
    adapter = make()
    provider.reply("native answer")
    view, _ = await collect(adapter, request())
    echoed = (await view.history())["messages"]
    next(m for m in echoed if m["role"] == "assistant")["content"] = "forged echo"
    provider.reply("next")
    await collect(
        adapter,
        request(
            [*echoed, {"id": "u2", "role": "user", "content": "Continue"}], run="r2"
        ),
    )
    contents = [m.get("content") for m in provider.requests[-1]["messages"]]
    assert "native answer" in contents
    assert "forged echo" not in contents


async def test_editing_accepted_user_input_is_rejected(setup):
    provider, make = setup
    adapter = make()
    provider.reply("native answer")
    await collect(adapter, request())
    _, events = await collect(
        adapter,
        request([{"id": "u1", "role": "user", "content": "Edited"}], run="edit"),
    )
    assert events[-1]["type"] == "RUN_ERROR"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("interrupt", [False, True])
async def test_continuation_uses_native_call_without_client_history(setup, interrupt):
    provider, make = setup
    definition = {
        "name": "browser",
        "description": "",
        "parameters": {"type": "object"},
    }
    adapter = make(interrupts=(InterruptTool(**definition),) if interrupt else ())
    provider.reply(calls=[("c1", "browser", "{}")])
    await collect(adapter, request(tools=[] if interrupt else [definition]))
    provider.reply("continued")
    data = request(run="r2", tools=[] if interrupt else [definition])
    data.messages = []
    if interrupt:
        from ag_ui.core import ResumeEntry

        data.resume = [ResumeEntry(interrupt_id="c1", status="resolved", payload=True)]
    else:
        data = request(
            [
                {
                    "id": "result",
                    "role": "tool",
                    "toolCallId": "c1",
                    "content": "browser result",
                }
            ],
            run="r2",
            tools=[definition],
        )
    _, events = await collect(adapter, data)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [
        m["content"] for m in provider.requests[-1]["messages"] if m["role"] == "user"
    ] == ["Hello"]
    assert [
        m["tool_call_id"]
        for m in provider.requests[-1]["messages"]
        if m["role"] == "tool"
    ] == ["c1"]


async def test_history_only_request_is_rejected_without_model_execution(setup):
    provider, make = setup
    adapter = make()
    provider.reply("native answer")
    view, _ = await collect(adapter, request())
    _, events = await collect(
        adapter, request((await view.history())["messages"], run="retry")
    )
    assert events[-1]["type"] == "RUN_ERROR"
    assert "No new user input" in events[-1]["message"]
    assert len(provider.requests) == 1


async def test_repeated_resume_does_not_silently_discard_new_input(setup):
    provider, make = setup
    adapter = make(
        interrupts=(InterruptTool("approve", "Approval", {"type": "object"}),)
    )
    provider.reply(calls=[("a1", "approve", "{}")])
    view, _ = await collect(adapter, request())
    resume = [{"interruptId": "a1", "status": "resolved", "payload": True}]
    provider.reply("done")
    await collect(
        adapter,
        request((await view.history())["messages"], run="resume", resume=resume),
    )
    new_user = [{"id": "next-user", "role": "user", "content": "Next question"}]
    _, events = await collect(adapter, request(new_user, run="retry", resume=resume))
    assert events[-1]["type"] == "RUN_ERROR"
    provider.reply("next answer")
    _, events = await collect(adapter, request(new_user, run="next"))
    assert events[-1]["type"] == "RUN_FINISHED"
