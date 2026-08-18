from __future__ import annotations

import atexit
import asyncio
import threading
import uuid
from pathlib import Path

import bub
from bub import hookimpl
from bub.builtin.settings import AgentSettings
from bub.turn import TurnState
from deepseek_harness import DeepSeekHarness, RunResult
from pydantic import Field
from pydantic_settings import SettingsConfigDict

ROOT_PROVIDER = "dsh"
DEFAULT_HARNESS_PROVIDER = "deepseek-official"


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
) -> str:
    runtime = _runtime_for(workspace)
    with runtime.lock:
        result: RunResult = runtime.harness.run(
            prompt,
            session_id=runtime.session_id(session_id),
        )
    if result.finish_reason == "error":
        raise DshRunError(_run_result_error(result))
    return result.final_response


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


@hookimpl
async def run_model(prompt: str | list[dict], session_id: str, state: TurnState) -> str:
    return await asyncio.to_thread(
        _run_with_dsh,
        prompt,
        session_id=session_id,
        workspace=workspace_from_state(state),
    )


__all__ = ["DshRunError", "DshSettings", "run_model"]
