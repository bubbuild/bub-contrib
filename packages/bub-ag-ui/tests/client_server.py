"""Deterministic real-socket fixture. Never contacts a model provider."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import aclosing

import httpx2
import republic
from ag_ui.core import ActivityDeltaEvent, ActivitySnapshotEvent
from bub.builtin.agent import BubAgent
from bub.builtin.settings import AgentSettings
from bub.core.store import InMemoryTapeStore
from bub.core.tools import Tool
from bub.framework import BubFramework
from bub_ag_ui.config import AGUISettings
from bub_ag_ui.http import create_app
from bub_ag_ui.runtime import AGUIAdapter
from bub_ag_ui.state import shared_state
from bub_ag_ui.tools import InterruptTool
from hypercorn.asyncio import serve
from hypercorn.config import Config


async def main(port):
    async def respond(request):
        data = json.loads(request.content)
        messages = data["messages"]
        prompt = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        delta = {"content": "Answer: " + prompt}
        if prompt == "reasoning":
            delta["reasoning_content"] = "Think first"
        if prompt == "error":
            return httpx2.Response(400, json={"error": {"message": "fixture failure"}})
        tool = {
            "backend": "clock",
            "frontend": "browser",
            "frontend-parallel": "browser",
            "interrupt": "approve",
            "state": "update",
            "activity": "activity",
            "cancel": "wait",
            "subagent": "delegate",
        }.get(prompt)
        if tool and messages[-1]["role"] == "user":
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-" + prompt,
                        "function": {"name": tool, "arguments": "{}"},
                    }
                ]
            }
        if prompt == "frontend-parallel" and "tool_calls" in delta:
            delta["tool_calls"].append(
                {
                    "index": 1,
                    "id": "call-browser-2",
                    "function": {"name": "browser", "arguments": "{}"},
                }
            )
        events = [
            {
                "choices": [
                    {
                        "delta": delta,
                        "finish_reason": "tool_calls"
                        if "tool_calls" in delta
                        else "stop",
                    }
                ]
            },
            "[DONE]",
        ]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(
                "data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n"
                for e in events
            ),
        )

    async def update(context):
        await shared_state(context).set({"count": 1})
        await shared_state(context).patch(
            [{"op": "replace", "path": "/count", "value": 2}]
        )
        return "updated"

    async def activity(context):
        state = shared_state(context)
        for activity_type, count, replace in [
            ("search", 1, True),
            ("ignored", 99, False),
            ("progress", 2, True),
            ("ignored", 99, False),
        ]:
            await state.emit(
                ActivitySnapshotEvent(
                    message_id="activity-1",
                    activity_type=activity_type,
                    content={"count": count},
                    replace=replace,
                )
            )
        await state.emit(
            ActivityDeltaEvent(
                message_id="activity-1",
                activity_type="progress",
                patch=[{"op": "replace", "path": "/count", "value": 3}],
            )
        )
        return "activity updated"

    async def delegate(context):
        # Native subagents share this agent's EventBus with the parent run.
        child = await context.state["_runtime_agent"].run_stream(
            session_id="temp/client-child",
            prompt="nested child",
            state={**context.state, "session_id": "temp/client-child"},
            allowed_tools=[],
            allowed_skills=[],
        )
        async with aclosing(child):
            async for _ in child:
                pass
        if child.error:
            raise child.error
        return "child result"

    async def wait():
        await asyncio.sleep(60)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        model = republic.get_model(
            "openai:test", http_client=client, api_format="chat", api_key="test"
        )
        AgentSettings.chat_model = lambda *args, **kwargs: model
        framework = BubFramework()
        framework.load_builtin_hooks()
        store = InMemoryTapeStore()
        tools = [
            Tool.from_callable(lambda: "12:00 UTC", name="clock"),
            Tool.from_callable(update, context=True),
            Tool.from_callable(activity, context=True),
            Tool.from_callable(wait),
            Tool.from_callable(delegate, context=True),
        ]

        def factory(*args, **kwargs):
            kwargs.setdefault("tape_store", store)
            return BubAgent(*args, tools=tools, skill_dirs=[], **kwargs)

        adapter = AGUIAdapter(
            framework,
            agent_factory=factory,
            interrupts=(
                InterruptTool(
                    "approve", "Approval", {"type": "object"}, {"type": "boolean"}
                ),
            ),
        )
        app = create_app(
            framework, settings=AGUISettings(), adapter=adapter, manage_framework=False
        )
        config = Config()
        config.bind = [f"127.0.0.1:{port}"]
        await serve(app, config)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
