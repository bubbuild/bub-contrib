"""Application extensions and other channels retain their observable behavior."""

import asyncio
from contextlib import aclosing

import pytest
from bub import hookimpl
from bub.builtin.agent import BubAgent
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.core.events import ToolCallDecision
from bub.core.tools import Tool
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub_ag_ui.plugin import AGUIPlugin

from .conftest import collect, request


async def test_application_tool_policy_is_reflected_in_client_results(setup):
    provider, make = setup
    adapter = make()

    class Hooks:
        @hookimpl
        def load_state(self, message):
            # Applications can bind session tools during state loading.
            message._runtime_agent.tools["bound"] = Tool.from_callable(
                lambda: "original", name="bound"
            )

        @hookimpl
        def before_tool_call(self, call):
            return ToolCallDecision.replace("hook-result")

    adapter.framework.plugin_manager.register(Hooks(), "observe-turn")
    provider.reply(calls=[("c1", "bound", "{}")])
    provider.reply("done")
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [e["content"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == [
        "hook-result"
    ]


@pytest.mark.parametrize("failure", ["save", "render", "dispatch"])
async def test_output_hook_failure_prevents_protocol_success(setup, failure):
    provider, make = setup
    adapter = make()

    class Hooks:
        @hookimpl
        def save_state(self):
            if failure == "save":
                raise RuntimeError("save failed")

        @hookimpl
        def render_outbound(self):
            if failure == "render":
                raise RuntimeError("render failed")

        @hookimpl
        def dispatch_outbound(self):
            if failure == "dispatch":
                raise RuntimeError("dispatch failed")
            return False

    adapter.framework.plugin_manager.register(Hooks(), "failing-output-hook")
    provider.reply("answer")
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_ERROR"
    assert not any(e["type"] == "RUN_FINISHED" for e in events)


async def test_gateway_keeps_router_and_does_not_capture_other_channels(setup):
    provider, make = setup
    adapter = make(bind_router=False)
    framework = adapter.framework
    framework.plugin_manager.register(AGUIPlugin(framework), "ag-ui")
    manager = ChannelManager(framework, system_prompt=None)
    framework.bind_channel_router(manager)
    # A second controller must not replace a gateway's router.
    second = make(bind_router=False)
    provider.reply("gateway")
    _, events = await collect(second, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert (
        next(e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")
        == "gateway"
    )
    other = ChannelMessage(session_id="other", channel="other", content="hi")
    other._runtime_agent = BubAgent(framework, tools=[], skill_dirs=[])
    provider.reply("other channel answer")
    result = await framework.process_inbound(other, stream_output=True)
    assert result.model_output == "other channel answer"
    assert not any(e.get("delta") == "other channel answer" for e in events)
    provider.reply("gateway still works")
    _, next_events = await collect(second, request(thread="another"))
    assert next_events[-1]["type"] == "RUN_FINISHED"
    assert any(e.get("delta") == "gateway still works" for e in next_events)


async def test_concurrent_threads_keep_event_routes_separate(setup):
    _provider, make = setup
    adapter = make()
    both = asyncio.Event()
    count = 0

    class ModelHook:
        @hookimpl(tryfirst=True)
        def run_model_stream(self, prompt):
            async def source():
                nonlocal count
                count += 1
                if count == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 2)
                yield StreamEvent("text", {"delta": "".join(prompt)})

            return AsyncStreamEvents(source())

    # An explicitly higher-priority model hook still goes through the same
    # framework stream routing. There is no global "current AG-UI run".
    adapter.framework.plugin_manager.register(ModelHook(), "custom-model")
    results = await asyncio.gather(
        *(
            collect(
                adapter,
                request(
                    [{"id": "u1", "role": "user", "content": label}],
                    thread=label,
                ),
            )
            for label in ("one", "two")
        )
    )
    for thread_id, (thread, events) in zip(["one", "two"], results, strict=True):
        assert (await thread.history())["messages"][-1]["content"] == thread_id
        assert events[-1]["type"] == "RUN_FINISHED"


@pytest.mark.parametrize("stream_output", [False, True])
async def test_installation_preserves_other_channel_scalar_provider(
    setup, stream_output
):
    provider, make = setup
    adapter = make(bind_router=False)
    framework = adapter.framework

    class ScalarModel:
        @hookimpl
        def run_model(self, prompt):
            return "selected scalar model"

    framework.plugin_manager.register(ScalarModel(), "scalar-model")
    message = ChannelMessage(session_id="other", channel="other", content="hi")
    message._runtime_agent = BubAgent(framework, tools=[], skill_dirs=[])
    result = await framework.process_inbound(message, stream_output=stream_output)
    assert result.model_output == "selected scalar model"
    assert provider.requests == []


@pytest.mark.parametrize("action", ["deny", "replace", "proceed"])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_external_calls_obey_native_policy(setup, action, interrupt):
    from bub_ag_ui.tools import InterruptTool

    provider, make = setup
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}}
    adapter = make(
        interrupts=(InterruptTool("browser", "", schema),) if interrupt else ()
    )
    tools = (
        []
        if interrupt
        else [{"name": "browser", "description": "", "parameters": schema}]
    )

    class Guard:
        @hookimpl
        def before_tool_call(self, call):
            if action == "deny":
                return ToolCallDecision.deny("blocked by application policy")
            if action == "replace":
                return ToolCallDecision.replace("already handled")
            return ToolCallDecision.proceed({"value": 7})

    adapter.framework.plugin_manager.register(Guard(), "tool-policy")
    provider.reply(calls=[("f1", "browser", '{"value":1}')])
    if action != "proceed":
        provider.reply("handled without frontend execution")
    _, events = await collect(adapter, request(tools=tools))
    assert events[-1]["type"] == "RUN_FINISHED"
    if action == "proceed":
        assert not any(e["type"] == "TOOL_CALL_RESULT" for e in events)
        if interrupt:
            assert events[-1]["outcome"]["interrupts"][0]["metadata"]["arguments"] == {
                "value": 7
            }
    else:
        results = [e["content"] for e in events if e["type"] == "TOOL_CALL_RESULT"]
        assert len(results) == 1
        assert (
            "blocked by application policy" if action == "deny" else "already handled"
        ) in results[0]


async def test_custom_provider_delegating_native_recording_does_not_duplicate_history(
    setup,
):
    provider, make = setup
    adapter = make()

    class DelegatingProvider:
        @hookimpl(tryfirst=True)
        async def run_model_stream(self, prompt, session_id, state):
            return await state["_runtime_agent"].run_stream(
                prompt=prompt, session_id=session_id, state=state
            )

    adapter.framework.plugin_manager.register(DelegatingProvider(), "delegate")
    provider.reply("recorded by native agent")
    thread, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [
        message["content"]
        for message in (await thread.history())["messages"]
        if message["role"] == "assistant"
    ] == ["recorded by native agent"]
    adapter.framework.plugin_manager.unregister(name="delegate")
    provider.reply("next")
    await collect(
        adapter,
        request(
            [
                *(await thread.history())["messages"],
                {"id": "u2", "role": "user", "content": "Continue"},
            ],
            run="r2",
        ),
    )
    assert [
        message["content"]
        for message in provider.requests[-1]["messages"]
        if message["role"] == "assistant"
    ] == ["recorded by native agent"]


async def test_custom_provider_output_is_displayed_without_model_tape_supplement(setup):
    provider, make = setup
    adapter = make()

    class CustomProvider:
        @hookimpl(tryfirst=True)
        def run_model_stream(self):
            async def source():
                yield StreamEvent("reasoning", {"delta": "checking"})
                yield StreamEvent(
                    "tool_call",
                    {"tool_calls": [{"id": "c1", "name": "lookup", "arguments": "{}"}]},
                )
                yield StreamEvent(
                    "tool_result", {"call_ids": ["c1"], "tool_results": ["found"]}
                )
                yield StreamEvent("final", {"ok": True, "tool_results": ["found"]})
                yield StreamEvent("text", {"delta": "custom answer"})
                yield StreamEvent("final", {"ok": True})

            return AsyncStreamEvents(source())

    adapter.framework.plugin_manager.register(CustomProvider(), "custom")
    thread, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [
        event["toolCallId"] for event in events if event["type"] == "TOOL_CALL_RESULT"
    ] == ["c1"]
    adapter.framework.plugin_manager.unregister(name="custom")
    provider.reply("next")
    await collect(
        adapter,
        request(
            [
                *(await thread.history())["messages"],
                {"id": "u2", "role": "user", "content": "Continue"},
            ],
            run="r2",
        ),
    )
    # Display-only provider output is not promoted into Bub's model context by
    # the client echo. The provider remains responsible for native recording.
    assert not any(
        m.get("content") == "custom answer" for m in provider.requests[-1]["messages"]
    )
    assert not any(
        m.get("tool_call_id") == "c1" for m in provider.requests[-1]["messages"]
    )


@pytest.mark.parametrize("frontend", [False, True])
async def test_child_run_events_do_not_escape_into_parent_turn(setup, frontend):
    """Subagents use run_stream on the same agent/EventBus, but a different tape."""
    provider, make = setup

    async def delegate(context):
        agent = context.state["_runtime_agent"]
        child = await agent.run_stream(
            session_id="temp/ag-ui-child",
            prompt="Child task",
            state={**context.state, "session_id": "temp/ag-ui-child"},
            allowed_tools=["child_clock"],
            allowed_skills=[],
        )
        async with aclosing(child):
            async for _ in child:
                pass
        if child.error:
            raise child.error
        return "child result"

    adapter = make(
        [
            Tool.from_callable(delegate, context=True),
            Tool.from_callable(lambda: "child clock", name="child_clock"),
        ]
    )

    calls = [("delegate-call", "delegate", "{}")]
    client_tools = []
    if frontend:
        calls.append(("frontend-call", "browser", "{}"))
        client_tools = [
            {"name": "browser", "description": "", "parameters": {"type": "object"}}
        ]
    provider.reply("Parent exchange", calls=calls)
    # Reuse a parent call ID on the child tape: turn identity, not IDs or step
    # numbers, decides which native completion belongs to the parent exchange.
    provider.reply(calls=[("frontend-call", "child_clock", "{}")])
    provider.reply("child answer")
    if not frontend:
        provider.reply("parent answer")
    thread, events = await collect(adapter, request(tools=client_tools))

    assert events[-1]["type"] == "RUN_FINISHED"
    assert [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == (
        [] if frontend else ["delegate-call"]
    )
    assert not any(e.get("delta") == "child answer" for e in events)

    if frontend:
        history = (await thread.history())["messages"]
        provider.reply("parent answer")
        _, resumed = await collect(
            adapter,
            request(
                [
                    *history,
                    {
                        "id": "browser-result",
                        "role": "tool",
                        "toolCallId": "frontend-call",
                        "content": "browser result",
                    },
                ],
                run="r2",
                tools=client_tools,
            ),
        )
        assert resumed[-1]["type"] == "RUN_FINISHED"

    messages = provider.requests[-1]["messages"]
    parent_exchange = next(m for m in messages if m.get("tool_calls"))
    assert parent_exchange["content"] == "Parent exchange"
    assert [call["id"] for call in parent_exchange["tool_calls"]] == [
        c[0] for c in calls
    ]
    assert [m["content"] for m in messages if m["role"] == "tool"] == (
        ["child result", "browser result"] if frontend else ["child result"]
    )


@pytest.mark.parametrize("interrupt", [False, True])
async def test_child_cannot_suspend_parent_external_tools(setup, interrupt):
    from bub_ag_ui.tools import InterruptTool

    provider, make = setup

    async def delegate(context):
        agent = context.state["_runtime_agent"]
        child = await agent.run_stream(
            session_id="temp/child-external",
            prompt="Child task",
            state={**context.state, "session_id": "temp/child-external"},
        )
        async with aclosing(child):
            async for _ in child:
                pass
        if child.error:
            raise child.error
        return "child result"

    definition = {
        "name": "browser",
        "description": "",
        "parameters": {"type": "object"},
    }
    tools = [] if interrupt else [definition]
    adapter = make(
        [Tool.from_callable(delegate, context=True)],
        interrupts=(InterruptTool(**definition),) if interrupt else (),
    )
    provider.reply("Parent exchange", calls=[("delegate-call", "delegate", "{}")])
    # Even a model hallucinating an unavailable parent tool must not suspend it.
    provider.reply(calls=[("child-call", "browser", "{}")])
    provider.reply("child answer")
    provider.reply("parent answer")
    thread, events = await collect(adapter, request(tools=tools))
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == [
        "delegate-call"
    ]
    assert "browser" in {t["function"]["name"] for t in provider.requests[0]["tools"]}
    assert "browser" not in {
        t["function"]["name"] for t in provider.requests[1]["tools"]
    }
    history = (await thread.history())["messages"]
    provider.reply("next answer")
    await collect(
        adapter,
        request(
            [*history, {"id": "u2", "role": "user", "content": "Continue"}],
            run="r2",
            tools=tools,
        ),
    )
    messages = provider.requests[-1]["messages"]
    assert not any(m.get("content") == "Child task" for m in messages)
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [
        "delegate-call"
    ]


async def test_output_processing_obeys_run_timeout(setup, advance_time):
    provider, make = setup
    adapter = make(max_run_seconds=600)
    save_started = asyncio.Event()

    class Output:
        @hookimpl
        async def save_state(self):
            save_started.set()
            await asyncio.Event().wait()

    adapter.framework.plugin_manager.register(Output(), "blocked-save")
    provider.reply("finished model")
    pending = asyncio.create_task(collect(adapter, request()))
    await save_started.wait()
    advance_time(601)
    _, events = await pending
    assert events[-1]["type"] == "RUN_ERROR"
    assert "timed out" in events[-1]["message"]
