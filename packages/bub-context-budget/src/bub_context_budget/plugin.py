from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import cache
from typing import Any

import bub
import tiktoken
from bub import hookimpl
from bub.hooks.interception import LlmCallRequest, LlmCallResult
from bub.turn import TurnState
from pydantic import Field
from pydantic_settings import SettingsConfigDict

CONFIG_NAME = "context-budget"
DEFAULT_MAX_CONTEXT_TOKENS = 200_000
DEFAULT_ENCODING_NAME = "o200k_base"
HANDOFF_TOOL_NAME = "tape.handoff"
HANDOFF_INSTRUCTION_MARKER = "<context_budget_exceeded>"
RESERVE_TOKENS = 16_384
STATE_BASELINE_KEY = "_context_budget_usage_baseline"


@dataclass(frozen=True)
class _UsageBaseline:
    model: str
    tool_names: tuple[str, ...]
    message_count: int
    message_digest: str
    total_tokens: int


@bub.config(name=CONFIG_NAME)
class ContextBudgetSettings(bub.Settings):
    model_config = SettingsConfigDict(env_prefix="BUB_CONTEXT_BUDGET_", extra="ignore")
    max_context_tokens: int = Field(default=DEFAULT_MAX_CONTEXT_TOKENS, gt=0)
    encoding_name: str = DEFAULT_ENCODING_NAME


@cache
def _get_encoding(name: str) -> tiktoken.Encoding:
    return tiktoken.get_encoding(name)


def estimate_context_tokens(
    messages: list[dict[str, Any]],
    *,
    encoding_name: str,
) -> int:
    if not messages:
        return 0
    encoding = _get_encoding(encoding_name)
    return len(encoding.encode(_serialize_messages(messages), disallowed_special=()))


def _serialize_messages(messages: list[dict[str, Any]]) -> str:
    return json.dumps(
        messages,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def _message_digest(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_serialize_messages(messages).encode()).hexdigest()


def _token_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage_total_tokens(usage: Mapping[str, Any] | None) -> int | None:
    if usage is None:
        return None
    total = _token_count(usage.get("total_tokens"))
    if total is not None:
        return total

    prompt = _token_count(usage.get("prompt_tokens"))
    if prompt is None:
        prompt = _token_count(usage.get("input_tokens"))
    completion = _token_count(usage.get("completion_tokens"))
    if completion is None:
        completion = _token_count(usage.get("output_tokens"))
    if prompt is None and completion is None:
        return None
    return (prompt or 0) + (completion or 0)


def _assistant_message(result: LlmCallResult) -> dict[str, Any]:
    if result.tool_calls:
        return {"role": "assistant", "content": "", "tool_calls": result.tool_calls}
    return {"role": "assistant", "content": result.text or ""}


def _estimate_request_tokens(
    request: LlmCallRequest,
    state: TurnState,
    *,
    encoding_name: str,
) -> int:
    baseline = state.get(STATE_BASELINE_KEY)
    if (
        not isinstance(baseline, _UsageBaseline)
        or baseline.model != request.model
        or baseline.tool_names != request.tool_names
        or len(request.messages) < baseline.message_count
        or _message_digest(request.messages[: baseline.message_count])
        != baseline.message_digest
    ):
        return estimate_context_tokens(
            request.messages,
            encoding_name=encoding_name,
        )

    trailing_messages = request.messages[baseline.message_count :]
    return baseline.total_tokens + estimate_context_tokens(
        trailing_messages,
        encoding_name=encoding_name,
    )


def _handoff_instruction(
    *,
    estimated_tokens: int,
    threshold: int,
    limit: int,
) -> str:
    return (
        f"{HANDOFF_INSTRUCTION_MARKER}\n"
        f"The estimated input context is {estimated_tokens} tokens, above the handoff "
        f"threshold of {threshold} tokens. The configured context limit is {limit} tokens, "
        f"with {RESERVE_TOKENS} tokens reserved. Before doing any other work, call the "
        "`tape.handoff` "
        "(`tape_handoff`) tool with name `context-budget` and a concise summary that "
        "preserves the current goal, progress, key decisions, modified files, and remaining "
        "work. Do not continue the task in this call.\n"
        "</context_budget_exceeded>"
    )


def _inject_system_instruction(
    messages: list[dict[str, Any]],
    instruction: str,
) -> list[dict[str, Any]]:
    if any(
        message.get("role") == "system"
        and isinstance(message.get("content"), str)
        and HANDOFF_INSTRUCTION_MARKER in message["content"]
        for message in messages
    ):
        return messages

    updated = [dict(message) for message in messages]
    for index, message in enumerate(updated):
        if message.get("role") != "system" or not isinstance(
            message.get("content"), str
        ):
            continue
        content = message["content"]
        updated[index] = {
            **message,
            "content": f"{content}\n\n{instruction}" if content else instruction,
        }
        return updated

    return [{"role": "system", "content": instruction}, *updated]


@hookimpl(trylast=True)
def before_llm_call(
    request: LlmCallRequest,
    state: TurnState,
) -> LlmCallRequest | None:
    settings = bub.ensure_config(ContextBudgetSettings)
    estimated_tokens = _estimate_request_tokens(
        request,
        state,
        encoding_name=settings.encoding_name,
    )
    threshold = max(0, settings.max_context_tokens - RESERVE_TOKENS)
    if estimated_tokens <= threshold:
        return None

    instruction = _handoff_instruction(
        estimated_tokens=estimated_tokens,
        threshold=threshold,
        limit=settings.max_context_tokens,
    )
    messages = _inject_system_instruction(request.messages, instruction)
    if messages is request.messages:
        return None
    return replace(
        request,
        messages=messages,
    )


@hookimpl
def after_llm_call(
    request: LlmCallRequest,
    result: LlmCallResult,
    state: TurnState,
) -> None:
    total_tokens = _usage_total_tokens(result.usage)
    if total_tokens is None or result.error is not None:
        return

    covered_messages = [*request.messages, _assistant_message(result)]
    state[STATE_BASELINE_KEY] = _UsageBaseline(
        model=request.model,
        tool_names=request.tool_names,
        message_count=len(covered_messages),
        message_digest=_message_digest(covered_messages),
        total_tokens=total_tokens,
    )
