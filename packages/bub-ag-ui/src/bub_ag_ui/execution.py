"""Scoped hooks and event handlers for a complete AG-UI inbound turn.

The framework owns load/build/run/save/render/dispatch. This plugin supplies a
model-stream hook for already-persisted protocol input, using public session and
step APIs. It does not override Agent.start or bypass the inbound lifecycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ag_ui.core import RunAgentInput
from bub import hookimpl
from bub.builtin.agent import BubAgent
from bub.builtin.settings import load_session_settings
from bub.channels.message import ChannelMessage
from bub.envelope import Envelope, field_of
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub.turn import TurnState

from bub_ag_ui.events import EventStream
from bub_ag_ui.state import SharedState
from bub_ag_ui.tools import ExternalTools
from bub_ag_ui.turn import stream_turn


@dataclass(kw_only=True)
class AGUIInboundMessage(ChannelMessage):
    _runtime_agent: BubAgent
    request: RunAgentInput
    model_tape: str
    workspace: Path
    shared: SharedState
    events: EventStream
    external: ExternalTools
    stream_complete: bool = False


def stream_events(
    message: Envelope, stream: AsyncIterable[StreamEvent]
) -> AsyncIterable[StreamEvent]:
    """Per-message routing works for native and third-party model hooks alike."""
    if not isinstance(message, AGUIInboundMessage):
        return stream

    async def iterate() -> AsyncIterator[StreamEvent]:
        async for event in stream:
            await message.events.consume(event)
            yield event
        if error := getattr(stream, "error", None):
            raise error
        message.stream_complete = True

    return iterate()


class ExecutionHooks:
    @hookimpl(tryfirst=True)
    async def load_state(self, message: Envelope) -> TurnState | None:
        if not isinstance(message, AGUIInboundMessage):
            return None
        # Read settings from the same thread tape the native turn writes.
        settings = await load_session_settings(
            message._runtime_agent.tape(message.model_tape)
        )
        return {
            **settings,
            "_ag_ui_inbound": message,
            "_runtime_agent": message._runtime_agent,
            "_runtime_workspace": str(message.workspace),
            "ag_ui_context": [
                item.model_dump(mode="json") for item in message.request.context
            ],
            "ag_ui_forwarded_props": deepcopy(message.request.forwarded_props),
            "ag_ui_state": message.shared,
        }

    @hookimpl(tryfirst=True)
    def build_prompt(self, message: Envelope) -> list[str] | None:
        if isinstance(message, AGUIInboundMessage):
            # Descriptive input for the framework's turn result/observers. The
            # execution hook consumes the request messages on its native tape;
            # this text must not be appended to that input a second time.
            return [message.content] if message.content else []
        return None

    # A named implementation adapts this channel without advertising a new
    # model provider. Bub's provider selection checks run_model[_stream] method
    # names before invoking hooks; adding that method would displace an existing
    # scalar provider even when this scoped implementation returned None.
    @hookimpl(specname="run_model_stream", tryfirst=True)
    def stream_protocol_turn(self, state: TurnState) -> AsyncStreamEvents | None:
        inbound = state.get("_ag_ui_inbound")
        return (
            stream_turn(inbound, state)
            if isinstance(inbound, AGUIInboundMessage)
            else None
        )


class StreamRouter:
    """Standalone router; a gateway instead routes through AGUIChannel."""

    wrap_stream = staticmethod(stream_events)

    async def dispatch_output(self, message: Envelope) -> bool:
        # Render/dispatch hooks run normally, but the aggregated answer must not
        # duplicate text already delivered by the stream. Controller owns errors.
        return field_of(message, "channel") == "ag-ui"

    async def quit(self, session_id: str) -> None:
        return None


def install_execution_hooks(framework: Any) -> None:
    """Support direct create_app callers as well as entry-point loading."""
    if not framework.plugin_manager.has_plugin("ag-ui.execution"):
        framework.plugin_manager.register(ExecutionHooks(), name="ag-ui.execution")
