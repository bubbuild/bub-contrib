from __future__ import annotations

from typing import Any

import pytest
from republic import AsyncStreamEvents, StreamEvent

from bub.tools import REGISTRY, tool
from bub_dynamic_workflows.runner import BubSubagentRunner
from bub_dynamic_workflows.spec import WorkflowNode


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run_stream(self, **kwargs: Any) -> AsyncStreamEvents:
        self.calls.append(kwargs)

        async def iterator():
            yield StreamEvent("text", {"delta": "agent result"})

        return AsyncStreamEvents(iterator())


@pytest.mark.asyncio
async def test_bub_subagent_runner_reuses_builtin_tool_resolution() -> None:
    tool_name = "tests.workflow_runner_tool"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name)
    def workflow_runner_tool() -> str:
        return "ok"

    agent = FakeAgent()
    runner = BubSubagentRunner({"_runtime_agent": agent, "session_id": "parent"}, tape_name="tape")
    node = WorkflowNode(id="review", prompt="run", allowed_tools=["tests_workflow_runner_tool"])

    result = await runner.run_node(prompt="run", node=node, run_id="run-1", attempt=2, item_index=3)

    assert result == "agent result"
    assert agent.calls[0]["session_id"] == "temp/workflow-run-1-review-2-3"
    assert agent.calls[0]["allowed_tools"] == {tool_name}

    REGISTRY.pop(tool_name, None)
