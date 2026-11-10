"""The AG-UI adapter participates in Bub's native hook/EventBus lifecycle."""

import asyncio
from contextlib import aclosing

import pytest
from bub import hookimpl
from bub.builtin.agent import BubAgent
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.core.events import StepStarted, ToolCallDecision
from bub.core.tools import Tool
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub_ag_ui.execution import install_execution_hooks
from bub_ag_ui.plugin import AGUIChannel

from .conftest import collect, request


async def test_tools_bound_by_load_state_use_native_policy_and_output_hooks(setup):
    provider, make = setup
    adapter = make()
    saved, rendered = [], []

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

        @hookimpl
        def save_state(self, model_output):
            saved.append(model_output)

        @hookimpl
        def render_outbound(self, model_output):
            rendered.append(model_output)

    adapter.framework.plugin_manager.register(Hooks(), "observe-turn")
    provider.reply(calls=[("c1", "bound", "{}")])
    provider.reply("done")
    _, events = await collect(adapter, request())
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [e["content"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == [
        "hook-result"
    ]
    assert saved == rendered == ["done"]


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
    channel = AGUIChannel(framework)
    manager = ChannelManager(framework, system_prompt=None)
    manager._channels["ag-ui"] = channel
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
        def run_model_stream(self, state):
            async def source():
                nonlocal count
                count += 1
                if count == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 2)
                yield StreamEvent(
                    "text", {"delta": state["_ag_ui_inbound"].request.thread_id}
                )

            return AsyncStreamEvents(source())

    # An explicitly higher-priority model hook still goes through the same
    # framework stream routing. There is no global "current AG-UI run".
    adapter.framework.plugin_manager.register(ModelHook(), "custom-model")
    results = await asyncio.gather(
        collect(adapter, request(thread="one")), collect(adapter, request(thread="two"))
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
    framework.plugin_manager.unregister(name="ag-ui.execution")

    class ScalarModel:
        @hookimpl
        def run_model(self, prompt):
            return "selected scalar model"

    framework.plugin_manager.register(ScalarModel(), "scalar-model")
    for index in range(2):
        message = ChannelMessage(
            session_id=f"other-{index}", channel="other", content="hi"
        )
        message._runtime_agent = BubAgent(framework, tools=[], skill_dirs=[])
        result = await framework.process_inbound(message, stream_output=stream_output)
        assert result.model_output == "selected scalar model"
        install_execution_hooks(framework)
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
    finished = []

    class Guard:
        @hookimpl
        def before_tool_call(self, call):
            if action == "deny":
                return ToolCallDecision.deny("blocked by application policy")
            if action == "replace":
                return ToolCallDecision.replace("already handled")
            return ToolCallDecision.proceed({"value": 7})

        @hookimpl
        def after_tool_call(self, call, result):
            finished.append(result)

    adapter.framework.plugin_manager.register(Guard(), "tool-policy")
    provider.reply(calls=[("f1", "browser", '{"value":1}')])
    if action != "proceed":
        provider.reply("handled without frontend execution")
    thread, events = await collect(adapter, request(tools=tools))
    assert events[-1]["type"] == "RUN_FINISHED"
    if action == "proceed":
        assert len((await thread.history())["pending"]) == 1
        assert (await thread.history())["pending"][0]["arguments"] == '{"value": 7}'
        assert not finished  # suspended is not a fabricated completion
    else:
        assert (await thread.history())["pending"] == []
        assert len(finished) == 1
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
        def run_model_stream(self, state):
            message = state["_ag_ui_inbound"]
            agent = message._runtime_agent

            async def source():
                async with agent.session(message.model_tape, state=state) as tape:
                    async for event in agent.run_once(
                        None, tape=tape, model=agent.settings.chat_model()
                    ):
                        yield event

            return AsyncStreamEvents(source())

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
    # A subsequent request explicitly supplies these messages. The adapter must
    # use that context, not substitute its own persisted display history.
    assert any(
        m.get("content") == "custom answer" for m in provider.requests[-1]["messages"]
    )
    assert any(m.get("tool_call_id") == "c1" for m in provider.requests[-1]["messages"])


@pytest.mark.parametrize("frontend", [False, True])
async def test_child_run_events_do_not_escape_into_parent_turn(setup, frontend):
    """Subagents use run_stream on the same agent/EventBus, but a different tape."""
    provider, make = setup
    observed_steps = []

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

    class ObserveSteps:
        @hookimpl
        def load_state(self, message):
            message._runtime_agent.events.on(StepStarted, observed_steps.append)

    adapter.framework.plugin_manager.register(ObserveSteps(), "observe-child-steps")
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

    assert len({id(event.tape) for event in observed_steps}) == 2
    expected_steps = ["1"] if frontend else ["1", "2"]
    assert [
        e["stepName"] for e in events if e["type"] == "STEP_STARTED"
    ] == expected_steps
    assert [
        e["stepName"] for e in events if e["type"] == "STEP_FINISHED"
    ] == expected_steps
    assert events[-1]["type"] == "RUN_FINISHED"
    assert [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == [
        "delegate-call"
    ]
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
    assert (await thread.history())["pending"] == []
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
