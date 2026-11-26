"""Expose Bub over the experimental ACP protocol v2.

V2 differs from v1 in how a turn is reported: ``session/prompt`` returns as soon as
the user message joins the conversation, and the turn's lifecycle is reported with
``state_update`` notifications. A prompt sent while a turn runs is injected into
that turn, the same way v1 clients steer through the ``_lody/session/steer``
extension. V2 has no client filesystem or terminal methods, so turns use Bub's own
tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from acp.exceptions import RequestError
from acp.experimental import v2
from acp.experimental.v2 import schema
from bub.envelope import Envelope
from bub.streaming import StreamEvent

from bub_acp_server.agent import (
    ACPAgentBase,
    ACPPromptBlock,
    ACPPromptRun,
    ACPSession,
    ACPStreamRouter,
    ACPStreamState,
    ACPToolCallState,
    StreamPayload,
    _block_value,
    _bub_version,
    _list_payload,
    _message_chat_id,
    _stringify,
    _tool_call_id,
    _tool_kind,
    _tool_name,
    _tool_raw_input,
    _tool_result_outcome,
    framework_steering_inbox,
)
from bub_acp_server.mcp import server_configs_v2

if TYPE_CHECKING:
    from acp.schema import SessionConfigOptionSelect
    from bub.framework import BubFramework

logger = logging.getLogger(__name__)

_CONTEXT_COMPACTION_TOOL = "tape.handoff"


class ACPV2StreamRouter(ACPStreamRouter):
    """Translate Bub stream events into ACP v2 session updates.

    V2 chunks carry a message ID. Consecutive chunks of the same kind share an ID;
    a change of kind or a tool call starts a new message.
    """

    def __init__(self, client: Any, *, context_window_size: int = 128_000) -> None:
        super().__init__(client, context_window_size=context_window_size)
        self._messages: dict[str, tuple[str, str]] = {}

    def wrap_stream(
        self, message: Envelope, stream: AsyncIterable[StreamEvent]
    ) -> AsyncIterable[StreamEvent]:
        self._messages.pop(_message_chat_id(message), None)
        return super().wrap_stream(message, stream)

    def pop_stream_state(self, session_id: str) -> ACPStreamState | None:
        self._messages.pop(session_id, None)
        return super().pop_stream_state(session_id)

    def message_id(self, session_id: str, kind: str) -> str:
        current = self._messages.get(session_id)
        if current is None or current[0] != kind:
            current = (kind, uuid4().hex)
            self._messages[session_id] = current
        return current[1]

    async def publish_event(self, session_id: str, event: StreamEvent) -> None:
        if event.kind not in ("text", "reasoning", "user_text"):
            await super().publish_event(session_id, event)
            return
        delta = str(event.data.get("delta", ""))
        if not delta:
            return
        state = self._stream_states.setdefault(session_id, ACPStreamState())
        if event.kind == "text":
            state.sent_text = True
        update_type = {
            "text": schema.AgentMessageChunk,
            "reasoning": schema.AgentThoughtChunk,
            "user_text": schema.UserMessageChunk,
        }[event.kind]
        await self._client.session_update(
            session_id,
            update_type(
                message_id=self.message_id(session_id, event.kind),
                content=schema.TextContentBlock(text=delta),
            ),
        )

    async def attach_terminal(
        self, session_id: str, command: str, terminal_id: str
    ) -> None:
        del session_id, command, terminal_id  # V2 has no client terminals.

    def _usage_update(self, *, size: int, used: int) -> object:
        return schema.UsageUpdate(size=size, used=used)

    async def _send_agent_text(self, session_id: str, text: str) -> None:
        if not text:
            return
        await self._client.session_update(
            session_id,
            schema.AgentMessageChunk(
                message_id=self.message_id(session_id, "text"),
                content=schema.TextContentBlock(text=text),
            ),
        )

    async def _send_tool_calls(self, session_id: str, data: StreamPayload) -> None:
        self._messages.pop(session_id, None)
        state = self._stream_states[session_id]
        state.pending_tools = []
        for call in _list_payload(data.get("tool_calls")):
            tool = ACPToolCallState(
                tool_id=_tool_call_id(state.next_tool_index, call),
                name=_tool_name(call),
            )
            state.next_tool_index += 1
            state.pending_tools.append(tool)
            if tool.name == _CONTEXT_COMPACTION_TOOL:
                await self._client.session_update(
                    session_id,
                    schema.SessionCompactionUpdate(
                        compaction_id=tool.tool_id, status="in_progress"
                    ),
                )
                continue
            raw_input = _tool_raw_input(call)
            title = tool.name
            if tool.name == "bash":
                command = _block_value(raw_input, "command")
                if isinstance(command, str) and command:
                    tool.command = command
                    title = command
            await self._client.session_update(
                session_id,
                schema.SessionToolCallUpdate(
                    tool_call_id=tool.tool_id,
                    name=tool.name,
                    title=title,
                    kind=_tool_kind(tool.name),
                    status="in_progress",
                    raw_input=raw_input,
                ),
            )

    async def _send_tool_results(self, session_id: str, data: StreamPayload) -> None:
        self._messages.pop(session_id, None)
        state = self._stream_states[session_id]
        for position, result in enumerate(_list_payload(data.get("tool_results"))):
            if position < len(state.pending_tools):
                tool = state.pending_tools[position]
            else:
                tool = ACPToolCallState(tool_id=f"tool-{state.next_tool_index}")
                state.next_tool_index += 1
            status, raw_output = _tool_result_outcome(result)
            if tool.name == _CONTEXT_COMPACTION_TOOL:
                await self._client.session_update(
                    session_id,
                    schema.SessionCompactionUpdate(
                        compaction_id=tool.tool_id,
                        status=status,
                        error=_stringify(raw_output) if status == "failed" else None,
                    ),
                )
                continue
            await self._client.session_update(
                session_id,
                schema.SessionToolCallUpdate(
                    tool_call_id=tool.tool_id,
                    status=status,
                    raw_output=raw_output,
                    content=[
                        schema.ContentToolCallContent(
                            content=schema.TextContentBlock(text=_stringify(raw_output))
                        )
                    ],
                ),
            )
        state.pending_tools = []


class BubACPV2Agent(ACPAgentBase):
    """Bub as an ACP v2 agent; parallel to the v1 ``BubACPAgent``."""

    _client: v2.AgentSideConnection | None
    _stream_router: ACPV2StreamRouter | None

    def on_connect(self, conn: v2.AgentSideConnection) -> None:
        self._client = conn
        self._stream_router = ACPV2StreamRouter(
            conn, context_window_size=self.settings.context_window_size
        )

    def _mcp_server_configs(
        self, servers: Sequence[object] | None, cwd: Path
    ) -> dict[str, dict[str, Any]]:
        return server_configs_v2(servers, cwd)

    async def initialize(
        self,
        protocol_version: int,
        info: schema.Implementation,
        capabilities: schema.ClientCapabilities | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        del protocol_version, info, capabilities, kwargs
        return schema.InitializeResponse(
            protocol_version=v2.PROTOCOL_VERSION,
            info=schema.Implementation(name="bub", title="Bub", version=_bub_version()),
            capabilities=schema.AgentCapabilities(
                session=schema.SessionCapabilities(
                    prompt=schema.PromptCapabilities(
                        image=schema.PromptImageCapabilities()
                    ),
                    mcp=schema.McpCapabilities(
                        stdio=schema.McpStdioCapabilities(),
                        http=schema.McpHttpCapabilities(),
                    ),
                    delete=schema.SessionDeleteCapabilities(),
                    additional_directories=schema.SessionAdditionalDirectoriesCapabilities(),
                )
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        del kwargs
        session = self._load_or_adopt_session(
            session_id=uuid4().hex,
            cwd=cwd,
            additional_directories=additional_directories,
        )
        try:
            await self._replace_session_mcp(session, mcp_servers)
        except BaseException:
            self._sessions.pop(session.session_id, None)
            self._save_sessions()
            raise
        await self._report_session_state(session.session_id)
        return schema.NewSessionResponse(
            session_id=session.session_id,
            config_options=await self._v2_config_options(session),
        )

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        replay_from: Any = None,
        **kwargs: Any,
    ) -> schema.ResumeSessionResponse:
        del kwargs
        async with self._prompt_lock:
            session = self._load_or_adopt_session(
                session_id=session_id,
                cwd=cwd,
                additional_directories=additional_directories,
            )
            await self._replace_session_mcp(session, mcp_servers)
            if isinstance(replay_from, schema.ReplayFromStartVariant):
                await self._attach_session_history(session)
            await self._report_session_state(session.session_id)
            return schema.ResumeSessionResponse(
                config_options=await self._v2_config_options(session)
            )

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **kwargs: Any
    ) -> schema.ListSessionsResponse:
        del cwd, cursor, kwargs
        sessions_on_disk = self._load_sessions()
        self._sessions.clear()
        self._sessions.update(sessions_on_disk)
        sessions = sorted(
            self._sessions.values(),
            key=lambda item: item.updated_at or "",
            reverse=True,
        )
        return schema.ListSessionsResponse(
            sessions=[_session_info(session) for session in sessions]
        )

    async def delete_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.DeleteSessionResponse:
        del kwargs
        await self._end_session(session_id, delete_tape=True)
        return schema.DeleteSessionResponse()

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse:
        del kwargs
        await self._end_session(session_id, delete_tape=False)
        return schema.CloseSessionResponse()

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: Any,
        *,
        type: str | None = None,
        **kwargs: Any,
    ) -> schema.SetSessionConfigOptionResponse:
        del type, kwargs
        session = self._sessions.get(session_id) or self._adopt_session(session_id)
        session.touch()
        try:
            config_options = await self._set_session_config_option(
                session, config_id, value
            )
        except ValueError as error:
            raise RequestError.invalid_params(
                {"configId": config_id, "reason": str(error)}
            ) from error
        self._save_sessions()
        return schema.SetSessionConfigOptionResponse(
            config_options=_to_v2_config_options(config_options)
        )

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> schema.PromptResponse:
        del kwargs
        if self._closed or session_id in self._closing_sessions:
            raise RequestError.invalid_request({"sessionId": session_id})
        session = self._sessions.get(session_id) or self._adopt_session(session_id)
        session.touch()
        self._save_sessions()
        message_id = uuid4().hex
        await self._require_client().session_update(
            session_id,
            schema.UserMessageUpdate(message_id=message_id, content=prompt),
        )
        task = asyncio.create_task(
            self._deliver_prompt(prompt, session_id),
            name=f"acp-v2-prompt-{session_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return schema.PromptResponse(message_id=message_id)

    async def cancel_session(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        self._cancel_prompt_runs(session_id)
        await self.framework.quit_via_channel_router(session_id)

    async def _deliver_prompt(
        self, prompt: list[ACPPromptBlock], session_id: str
    ) -> None:
        """Inject the prompt into the running turn, or start a turn with it."""
        try:
            await self._execute_or_queue_steering(session_id, prompt)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("ACP v2 prompt was not delivered")
            await self._send_notice(session_id, "Prompt was not delivered", error)

    async def _execute_prompt(
        self,
        prompt: list[ACPPromptBlock],
        session: ACPSession,
        run: ACPPromptRun,
    ) -> None:
        try:
            idle = await self._run_reported_turn(prompt, session, run)
            try:
                # Report idle before releasing the run so a queued prompt's next
                # turn reports running after it.
                await self._require_client().session_update(session.session_id, idle)
            except Exception:
                logger.exception("Failed to report ACP v2 idle state")
        finally:
            self._complete_prompt_run(run)

    async def _run_reported_turn(
        self,
        prompt: list[ACPPromptBlock],
        session: ACPSession,
        run: ACPPromptRun,
    ) -> schema.IdleSessionStateUpdate:
        """Run a turn after reporting running; return the idle update to report."""
        try:
            await self._require_client().session_update(
                session.session_id, schema.RunningSessionStateUpdate()
            )
            stream_state = await self._run_turn(prompt, session, run)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            # Only session/cancel ends with a state update; close and shutdown cancel.
            if not run.cancel_requested or task is None or task.uncancel() > 0:
                raise
            return schema.IdleSessionStateUpdate(stop_reason="cancelled")
        except Exception as error:
            logger.exception("ACP v2 turn failed")
            # A turn that never started is reported by the prompt's delivery.
            if run.started.is_set():
                await self._send_notice(session.session_id, "Turn failed", error)
            return schema.IdleSessionStateUpdate()
        stop_reason = "end_turn"
        if run.cancel_requested:
            # The turn swallowed the cancellation; clear it before reporting.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                task.uncancel()
            stop_reason = "cancelled"
        turn_usage = stream_state.prompt_usage() if stream_state else None
        if turn_usage is None:
            return schema.IdleSessionStateUpdate(stop_reason=stop_reason)
        return schema.IdleSessionStateUpdate(
            stop_reason=stop_reason,
            usage=schema.Usage.model_validate(turn_usage.model_dump(exclude_none=True)),
        )

    async def _report_session_state(self, session_id: str) -> None:
        run = self._current_prompt_run(session_id)
        update = (
            schema.IdleSessionStateUpdate()
            if run is None
            else schema.RunningSessionStateUpdate()
        )
        await self._require_client().session_update(session_id, update)

    async def _send_final_text(self, session_id: str, text: str) -> None:
        router = self._require_stream_router()
        await self._require_client().session_update(
            session_id,
            schema.AgentMessageChunk(
                message_id=router.message_id(session_id, "text"),
                content=schema.TextContentBlock(text=text),
            ),
        )

    async def _send_notice(
        self, session_id: str, title: str, error: BaseException
    ) -> None:
        with contextlib.suppress(Exception):
            await self._require_client().session_update(
                session_id,
                schema.SessionNotice(
                    severity="error", title=title, description=str(error) or None
                ),
            )

    async def _v2_config_options(
        self, session: ACPSession
    ) -> list[schema.SelectSessionConfigOption]:
        return _to_v2_config_options(await self._session_config_options(session))


def _session_info(session: ACPSession) -> schema.SessionInfo:
    return schema.SessionInfo(
        session_id=session.session_id,
        cwd=str(session.cwd),
        additional_directories=session.additional_directories or None,
        title=session.title,
        updated_at=session.updated_at,
    )


def _to_v2_config_options(
    options: list[SessionConfigOptionSelect],
) -> list[schema.SelectSessionConfigOption]:
    return [
        schema.SelectSessionConfigOption(
            config_id=option.id,
            name=option.name,
            description=option.description,
            category=option.category,
            current_value=option.current_value,
            options=[
                schema.SessionConfigSelectOption(
                    value=choice.value,
                    name=choice.name,
                    description=choice.description,
                    field_meta=choice.field_meta,
                )
                for choice in option.options
            ],
            field_meta=option.field_meta,
        )
        for option in options
    ]


async def run_acp_v2_agent(framework: BubFramework) -> None:
    agent = BubACPV2Agent(framework)
    async with framework.running():
        if (steering_inbox := framework_steering_inbox(framework)) is not None:
            agent.set_steering_inbox(steering_inbox)
        try:
            await v2.run_agent(agent)
        finally:
            await agent.shutdown()
