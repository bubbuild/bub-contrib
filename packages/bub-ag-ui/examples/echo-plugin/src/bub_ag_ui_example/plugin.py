from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from bub import hookimpl
from bub.streaming import AsyncStreamEvents, StreamEvent, StreamState


class EchoModel:
    """Return a deterministic response while exercising Bub's streaming path."""

    @hookimpl
    async def run_model_stream(
        self,
        prompt: str | list[dict[str, Any]],
        session_id: str,
        state: dict[str, Any],
    ) -> AsyncStreamEvents:
        del session_id, state
        response = f"Bub received through AG-UI: {_prompt_text(prompt)}"

        async def events() -> AsyncIterator[StreamEvent]:
            yield StreamEvent("text", {"delta": response})
            yield StreamEvent("final", {"text": response, "ok": True})

        return AsyncStreamEvents(events(), state=StreamState())


def main(framework: Any) -> EchoModel:
    del framework
    return EchoModel()


def _prompt_text(prompt: str | list[dict[str, Any]]) -> str:
    if isinstance(prompt, str):
        return prompt
    return json.dumps(prompt, ensure_ascii=False)
