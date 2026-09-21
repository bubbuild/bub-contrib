from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from bub.model_selection import ModelOptions
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState
from bub.tools import ToolContext
from bub.turn import TurnResult

from bub_acp_server.agent import run_acp_agent


class E2ETape:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def append_event(
        self, name: str, payload: dict[str, object], **meta: object
    ) -> None:
        self.events.append({"name": name, "payload": payload, "meta": meta})


class E2EFramework:
    def get_agent_hooks(self):
        return None

    def __init__(self) -> None:
        self.workspace = Path(os.environ["BUB_ACP_E2E_WORKSPACE"]).resolve()
        self._channel_router: Any = None

    @asynccontextmanager
    async def running(self):
        yield

    def bind_channel_router(self, router: Any) -> None:
        self._channel_router = router

    async def quit_via_channel_router(self, session_id: str) -> None:
        del session_id

    async def get_model_options(
        self, *, session_id: str, workspace: Path
    ) -> ModelOptions:
        del session_id, workspace
        return ModelOptions()

    async def process_inbound(
        self, inbound: Any, stream_output: bool = False
    ) -> TurnResult:
        assert stream_output is True
        tape = E2ETape()
        context = ToolContext(
            tape=cast(Any, tape),
            run_id="e2e-run",
            state={
                "session_id": inbound.session_id,
                "_runtime_workspace": str(self.workspace),
            },
        )
        read_result = await inbound._runtime_agent.tools["fs.read"].run(
            path="target.txt", offset=1, limit=1, context=context
        )
        write_result = await inbound._runtime_agent.tools["fs.write"].run(
            path="created.txt", content="created by ACP", context=context
        )
        edit_result = await inbound._runtime_agent.tools["fs.edit"].run(
            path="target.txt",
            old="old value",
            new="new value",
            start=1,
            context=context,
        )
        bash_result = await inbound._runtime_agent.tools["bash"].run(
            cmd="printf e2e-command", context=context
        )
        plan_result = await inbound._runtime_agent.tools["update_plan"].run(
            explanation="Exercise ACP plan updates",
            plan=[
                {"step": "Exercise client tools", "status": "completed"},
                {"step": "Verify results", "status": "in_progress"},
            ],
            context=context,
        )
        ask_result = None
        if "ask_user" in inbound._runtime_agent.tools:
            ask_result = await inbound._runtime_agent.tools["ask_user"].run(
                message="Choose an approach",
                requested_schema={
                    "type": "object",
                    "properties": {
                        "answer": {"type": "string", "enum": ["minimal", "full"]}
                    },
                    "required": ["answer"],
                },
                context=context,
            )
        model_output = json.dumps(
            {
                "read": read_result,
                "write": write_result,
                "edit": edit_result,
                "bash": bash_result,
                "plan": plan_result,
                "tape_events": tape.events,
                "ask_user": ask_result,
            },
            sort_keys=True,
        )

        async def stream():
            yield StreamEvent("text", {"delta": model_output})
            yield StreamEvent("final", {"text": model_output, "ok": True})

        events = AsyncStreamEvents(
            stream(),
            state=StreamState(usage={"prompt_tokens": 21, "completion_tokens": 13}),
        )
        async for _ in self._channel_router.wrap_stream(inbound, events):
            pass
        return TurnResult(
            session_id=inbound.session_id,
            prompt=inbound.content,
            model_output=model_output,
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_acp_agent(E2EFramework()))
