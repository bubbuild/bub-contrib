from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import acp
from acp import schema
from bub import BubFramework
from bub.builtin.agent import Agent
from republic import AsyncStreamEvents, RepublicError, StreamEvent

from bub_acp.bridge import ACP_PROTOCOL_VERSION, _render_block_as_text, prompt_from_acp_blocks


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _default_modes() -> schema.SessionModeState:
    return schema.SessionModeState(
        available_modes=[schema.SessionMode(id="default", name="default", description="Default Bub mode")],
        current_mode_id="default",
    )


def _default_models() -> schema.SessionModelState:
    return schema.SessionModelState(
        available_models=[schema.ModelInfo(model_id="bub", name="Bub", description="Bub hook runtime")],
        current_model_id="bub",
    )


class PromptRunner(Protocol):
    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str | list[dict[str, Any]],
        cwd: Path,
    ) -> AsyncStreamEvents: ...


@dataclass
class ACPServerSession:
    session_id: str
    cwd: Path
    title: str | None
    updated_at: str


class BubHookPromptRunner:
    async def run_stream(
        self,
        *,
        session_id: str,
        prompt: str | list[dict[str, Any]],
        cwd: Path,
    ) -> AsyncStreamEvents:
        framework = BubFramework()
        framework.workspace = cwd.resolve()
        framework.load_hooks()
        runtime_agent = Agent(framework)
        state = {
            "_runtime_workspace": str(framework.workspace),
            "_runtime_agent": runtime_agent,
        }

        if isinstance(prompt, str) and prompt.strip().startswith(","):
            return await runtime_agent.run_stream(
                session_id=session_id,
                prompt=prompt,
                state=state,
            )

        stream = await framework._hook_runtime.run_model_stream(
            prompt=prompt,
            session_id=session_id,
            state=state,
        )
        if stream is not None:
            return stream

        result = await framework._hook_runtime.run_model(
            prompt=prompt,
            session_id=session_id,
            state=state,
        )
        if result is not None:
            async def iterator() -> AsyncIterator[StreamEvent]:
                yield StreamEvent("text", {"delta": result})
                yield StreamEvent("final", {"text": result, "tool_calls": [], "tool_results": [], "ok": True})

            return AsyncStreamEvents(iterator())

        return await runtime_agent.run_stream(
            session_id=session_id,
            prompt=prompt,
            state=state,
        )


class BubACPServerAgent(acp.Agent):
    def __init__(self, runner: PromptRunner | None = None) -> None:
        self.runner = runner or BubHookPromptRunner()
        self._client: acp.Client | None = None
        self._sessions: dict[str, ACPServerSession] = {}
        self._active_prompts: dict[str, asyncio.Task[schema.PromptResponse]] = {}

    def on_connect(self, conn: acp.Client) -> None:
        self._client = conn

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        return schema.InitializeResponse(
            protocol_version=protocol_version if protocol_version >= ACP_PROTOCOL_VERSION else ACP_PROTOCOL_VERSION,
            agent_info=schema.Implementation(name="bub-acp", title="Bub", version="0.1.0"),
            agent_capabilities=schema.AgentCapabilities(
                load_session=True,
                prompt_capabilities=schema.PromptCapabilities(
                    image=True,
                    embedded_context=True,
                    audio=False,
                ),
                session_capabilities=schema.SessionCapabilities(
                    list=schema.SessionListCapabilities(),
                    resume=schema.SessionResumeCapabilities(),
                    fork=schema.SessionForkCapabilities(),
                    close=schema.SessionCloseCapabilities(),
                ),
            ),
        )

    async def new_session(
        self,
        cwd: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        session = self._create_session(Path(cwd))
        return schema.NewSessionResponse(
            session_id=session.session_id,
            modes=_default_modes(),
            models=_default_models(),
        )

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.LoadSessionResponse | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.cwd = Path(cwd).resolve()
        session.updated_at = _iso_now()
        return schema.LoadSessionResponse(modes=_default_modes(), models=_default_models())

    async def list_sessions(
        self,
        cursor: str | None = None,
        cwd: str | None = None,
        **kwargs: Any,
    ) -> schema.ListSessionsResponse:
        sessions = []
        for session in self._sessions.values():
            if cwd is not None and session.cwd != Path(cwd).resolve():
                continue
            sessions.append(
                schema.SessionInfo(
                    session_id=session.session_id,
                    cwd=str(session.cwd),
                    title=session.title,
                    updated_at=session.updated_at,
                )
            )
        return schema.ListSessionsResponse(sessions=sessions)

    async def resume_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.ResumeSessionResponse:
        session = self._require_session(session_id)
        session.cwd = Path(cwd).resolve()
        session.updated_at = _iso_now()
        return schema.ResumeSessionResponse(modes=_default_modes(), models=_default_models())

    async def fork_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.ForkSessionResponse:
        parent = self._require_session(session_id)
        child = self._create_session(Path(cwd), title=parent.title)
        return schema.ForkSessionResponse(
            session_id=child.session_id,
            modes=_default_modes(),
            models=_default_models(),
        )

    async def close_session(self, session_id: str, **kwargs: Any) -> schema.CloseSessionResponse:
        self._sessions.pop(session_id, None)
        task = self._active_prompts.pop(session_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return schema.CloseSessionResponse()

    async def set_session_mode(
        self,
        mode_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> schema.SetSessionModeResponse:
        self._require_session(session_id)
        return schema.SetSessionModeResponse()

    async def set_session_model(
        self,
        model_id: str,
        session_id: str,
        **kwargs: Any,
    ) -> schema.SetSessionModelResponse:
        self._require_session(session_id)
        return schema.SetSessionModelResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        task = self._active_prompts.get(session_id)
        if task is not None:
            task.cancel()

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> schema.PromptResponse:
        session = self._require_session(session_id)
        session.updated_at = _iso_now()
        if session.title is None:
            session.title = _title_from_prompt(prompt)

        task = asyncio.create_task(
            self._prompt_impl(prompt=prompt, session=session, message_id=message_id)
        )
        self._active_prompts[session_id] = task
        try:
            return await task
        finally:
            self._active_prompts.pop(session_id, None)

    async def _prompt_impl(
        self,
        *,
        prompt: list[Any],
        session: ACPServerSession,
        message_id: str | None,
    ) -> schema.PromptResponse:
        client = self._client
        rendered_prompt = prompt_from_acp_blocks(prompt)
        text_parts: list[str] = []
        usage: schema.Usage | None = None
        stream = await self.runner.run_stream(
            session_id=session.session_id,
            prompt=rendered_prompt,
            cwd=session.cwd,
        )
        message_uuid = str(uuid.uuid4())
        try:
            async for event in stream:
                if event.kind == "text":
                    delta = str(event.data.get("delta", ""))
                    if not delta:
                        continue
                    text_parts.append(delta)
                    if client is not None:
                        await client.session_update(
                            session.session_id,
                            schema.AgentMessageChunk(
                                session_update="agent_message_chunk",
                                message_id=message_uuid,
                                content=acp.text_block(delta),
                            ),
                        )
                    continue
                if event.kind == "tool_call":
                    if client is None:
                        continue
                    await client.session_update(
                        session.session_id,
                        acp.start_tool_call(
                            tool_call_id=str(event.data.get("call", {}).get("id") or event.data.get("index")),
                            title=str(event.data.get("call", {}).get("title") or "tool"),
                            kind=event.data.get("call", {}).get("kind"),
                            status=event.data.get("call", {}).get("status") or "in_progress",
                            raw_input=event.data.get("call", {}).get("raw_input"),
                        ),
                    )
                    continue
                if event.kind == "tool_result":
                    if client is None:
                        continue
                    result = event.data.get("result", {})
                    await client.session_update(
                        session.session_id,
                        acp.update_tool_call(
                            tool_call_id=str(result.get("id") or event.data.get("index")),
                            title=result.get("title"),
                            kind=result.get("kind"),
                            status=result.get("status") or "completed",
                            raw_output=result.get("raw_output"),
                        ),
                    )
                    continue
                if event.kind == "usage":
                    used = int(event.data.get("used", 0))
                    usage = schema.Usage(
                        input_tokens=used,
                        output_tokens=0,
                        total_tokens=used,
                    )
                    if client is not None:
                        await client.session_update(
                            session.session_id,
                            schema.UsageUpdate(
                                session_update="usage_update",
                                used=int(event.data.get("used", 0)),
                                size=int(event.data.get("size", 0)),
                            ),
                        )
                    continue
                if event.kind == "error":
                    if client is not None:
                        await client.session_update(
                            session.session_id,
                            schema.AgentMessageChunk(
                                session_update="agent_message_chunk",
                                message_id=message_uuid,
                                content=acp.text_block(f"error: {event.data.get('message', 'unknown error')}"),
                            ),
                        )
        except asyncio.CancelledError:
            return schema.PromptResponse(
                stop_reason="cancelled",
                usage=usage,
                user_message_id=message_id,
            )

        return schema.PromptResponse(
            stop_reason="end_turn",
            usage=usage,
            user_message_id=message_id,
        )

    def _create_session(self, cwd: Path, *, title: str | None = None) -> ACPServerSession:
        session = ACPServerSession(
            session_id=str(uuid.uuid4()),
            cwd=cwd.resolve(),
            title=title,
            updated_at=_iso_now(),
        )
        self._sessions[session.session_id] = session
        return session

    def _require_session(self, session_id: str) -> ACPServerSession:
        if session_id not in self._sessions:
            raise KeyError(session_id)
        return self._sessions[session_id]


def _title_from_prompt(prompt: list[Any]) -> str | None:
    rendered = " ".join(_render_block_as_text(block) for block in prompt).strip()
    if not rendered:
        return None
    return rendered[:80]


def make_server_agent() -> BubACPServerAgent:
    return BubACPServerAgent()
