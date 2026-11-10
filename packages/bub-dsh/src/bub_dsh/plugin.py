from __future__ import annotations

import atexit
import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import bub
from bub import hookimpl
from bub.builtin.settings import AgentSettings
from bub.streaming import AsyncStreamEvents, StreamEvent
from bub.turn import TurnState
from deepseek_harness import DeepSeekHarness, Notification, RunResult
from pydantic import Field
from pydantic_settings import SettingsConfigDict

ROOT_PROVIDER = "dsh"
DEFAULT_HARNESS_PROVIDER = "deepseek-official"
_STREAM_DONE = object()


class _HarnessRuntime:
    def __init__(self, kwargs: dict[str, object]) -> None:
        self.harness = DeepSeekHarness(**kwargs)
        self.lock = threading.Lock()
        self.session_ids: dict[str, str] = {}

    def session_id(self, bub_session_id: str) -> str:
        return self.session_ids.setdefault(
            bub_session_id,
            f"bub-{uuid.uuid4().hex}",
        )


_runtimes: dict[tuple[tuple[str, object], ...], _HarnessRuntime] = {}
_runtimes_lock = threading.Lock()


class DshRunError(RuntimeError):
    """Error reported by a completed DeepSeek Harness run."""

    def __init__(self, error: dict[str, object] | None = None) -> None:
        self.error = error
        message = "DeepSeek Harness run failed"
        if error:
            detail = error.get("message")
            code = error.get("code")
            if isinstance(detail, str) and detail:
                message = f"{message}: {detail}"
            if isinstance(code, str) and code:
                message = f"{message} [{code}]"
        super().__init__(message)


@bub.config(name="dsh")
class DshSettings(bub.Settings):
    """Configuration for the DeepSeek Harness Bub plugin."""

    model_config = SettingsConfigDict(env_prefix="BUB_DSH_", extra="ignore")

    request_timeout_seconds: float | None = Field(default=None, gt=0)
    shutdown_timeout_seconds: float = Field(default=1.0, gt=0)
    cordis: str | None = None
    session_root: Path = Field(default_factory=lambda: bub.home / "dsh")


def workspace_from_state(state: TurnState) -> Path:
    raw = state.get("_runtime_workspace")
    if isinstance(raw, str) and raw.strip():
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def _split_root_model(model: str) -> tuple[str, str]:
    provider, separator, model_id = model.partition(":")
    if not separator:
        provider, model_id = ROOT_PROVIDER, provider
    provider = provider.strip()
    model_id = model_id.strip()
    if not provider or not model_id:
        raise RuntimeError(f"Invalid Bub model identifier: {model!r}")
    return provider, model_id


def _provider_value(
    value: str | dict[str, str] | None,
    provider: str,
) -> str | None:
    if isinstance(value, dict):
        return value.get(provider)
    return value


def _harness_kwargs(workspace: Path) -> dict[str, object]:
    settings = bub.ensure_config(DshSettings)
    root_settings = bub.ensure_config(AgentSettings)
    root_provider, model_id = _split_root_model(root_settings.model)
    kwargs: dict[str, object] = {
        "provider": (
            DEFAULT_HARNESS_PROVIDER
            if root_provider == ROOT_PROVIDER
            else root_provider
        ),
        "model": model_id,
        "max_tokens": root_settings.max_tokens,
        "cwd": str(workspace),
        "session_root": str(settings.session_root.expanduser().resolve()),
        "request_timeout_seconds": settings.request_timeout_seconds,
        "shutdown_timeout_seconds": settings.shutdown_timeout_seconds,
    }
    if settings.cordis:
        kwargs["cordis"] = settings.cordis
    if api_base := _provider_value(root_settings.api_base, root_provider):
        kwargs["base_url"] = api_base
    if api_key := _provider_value(root_settings.api_key, root_provider):
        kwargs["api_key"] = api_key
    return kwargs


def _runtime_for(workspace: Path) -> _HarnessRuntime:
    kwargs = _harness_kwargs(workspace)
    key = tuple(sorted(kwargs.items()))
    with _runtimes_lock:
        runtime = _runtimes.get(key)
        if runtime is None:
            runtime = _HarnessRuntime(kwargs)
            _runtimes[key] = runtime
        return runtime


def _close_runtimes() -> None:
    with _runtimes_lock:
        runtimes = list(_runtimes.values())
        _runtimes.clear()
    for runtime in runtimes:
        runtime.harness.close()


atexit.register(_close_runtimes)


def _run_with_dsh(
    prompt: str | list[dict],
    *,
    session_id: str,
    workspace: Path,
    on_stream_event: Callable[[StreamEvent], None],
) -> None:
    runtime = _runtime_for(workspace)
    harness_session_id = runtime.session_id(session_id)
    text_emitted = False

    def on_notification(notification: Notification) -> None:
        nonlocal text_emitted
        event = _stream_event_from_notification(notification, harness_session_id)
        if event is None:
            return
        if event.kind == "text":
            text_emitted = True
        on_stream_event(event)

    with runtime.lock:
        result: RunResult = runtime.harness.run(
            prompt,
            session_id=harness_session_id,
            on_notification=on_notification,
        )
    if result.finish_reason == "error":
        raise DshRunError(_run_result_error(result))
    if not text_emitted and result.final_response:
        on_stream_event(StreamEvent("text", {"delta": result.final_response}))


def _stream_event_from_notification(
    notification: Notification,
    session_id: str,
) -> StreamEvent | None:
    if notification.method != "session.event":
        return None
    if notification.payload.get("sessionId") != session_id:
        return None
    event = notification.payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "assistant/chunk":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    chunk = data.get("chunk")
    if not isinstance(chunk, dict):
        return None
    chunk_type = chunk.get("type")
    text = chunk.get("text")
    if not isinstance(text, str) or not text:
        return None
    if chunk_type == "text-delta":
        return StreamEvent("text", {"delta": text})
    if chunk_type == "reasoning-delta":
        return StreamEvent("reasoning", {"delta": text})
    return None


def _run_result_error(result: RunResult) -> dict[str, object] | None:
    for event in reversed(result.events):
        if event.get("type") != "turn/end":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        reason = data.get("reason")
        if not isinstance(reason, dict) or reason.get("kind") != "error":
            continue
        error = reason.get("error")
        if isinstance(error, dict):
            return error
    return None


async def _stream_with_dsh(
    prompt: str | list[dict],
    *,
    session_id: str,
    workspace: Path,
) -> AsyncIterator[StreamEvent]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[StreamEvent | Exception | object] = asyncio.Queue()
    accepting_events = threading.Event()
    accepting_events.set()

    def enqueue(item: StreamEvent | Exception | object) -> None:
        if not accepting_events.is_set():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            pass

    def run() -> None:
        try:
            _run_with_dsh(
                prompt,
                session_id=session_id,
                workspace=workspace,
                on_stream_event=enqueue,
            )
        except Exception as error:
            enqueue(error)
        finally:
            enqueue(_STREAM_DONE)

    worker = asyncio.create_task(asyncio.to_thread(run))
    try:
        while True:
            item = await queue.get()
            if item is _STREAM_DONE:
                await worker
                return
            if isinstance(item, Exception):
                raise item
            if isinstance(item, StreamEvent):
                yield item
    finally:
        accepting_events.clear()


@hookimpl
async def run_model_stream(
    prompt: str | list[dict],
    session_id: str,
    state: TurnState,
) -> AsyncStreamEvents:
    return AsyncStreamEvents(
        _stream_with_dsh(
            prompt,
            session_id=session_id,
            workspace=workspace_from_state(state),
        )
    )


__all__ = ["DshRunError", "DshSettings", "run_model_stream"]
