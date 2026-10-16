from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest
import republic
from bub.hooks.interception import LLMCallRequest, LLMCallResult
from pydantic import ValidationError

from bub_context_budget import plugin


class CharacterEncoding:
    def encode(
        self,
        text: str,
        *,
        disallowed_special: tuple[()] = (),
    ) -> list[int]:
        del disallowed_special
        return list(range(len(text)))


def _request(messages: list[republic.Message]) -> LLMCallRequest:
    return LLMCallRequest(
        run_id="run-1",
        model="openai:gpt-5",
        messages=messages,
        tool_names=(plugin.HANDOFF_TOOL_NAME,),
        max_tokens=1024,
    )


def test_estimate_context_tokens_uses_canonical_unicode_payload(monkeypatch) -> None:
    monkeypatch.setattr(plugin, "_get_encoding", lambda _: CharacterEncoding())
    messages = [republic.user("你好")]

    result = plugin.estimate_context_tokens(messages, encoding_name="test")

    assert result == len('[{"content":"你好","role":"user"}]')


def test_estimate_context_tokens_returns_zero_for_no_messages() -> None:
    assert plugin.estimate_context_tokens([], encoding_name="unused") == 0


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"total_tokens": 123, "prompt_tokens": 100}, 123),
        ({"total_tokens": 0}, 0),
        ({"prompt_tokens": 100, "completion_tokens": 23}, 123),
        ({"input_tokens": 100, "output_tokens": 23}, 123),
        ({"input_tokens": 100}, 100),
        ({}, None),
    ],
)
def test_usage_total_tokens(usage: dict[str, int], expected: int | None) -> None:
    assert plugin._usage_total_tokens(usage) == expected


def test_real_usage_is_combined_with_trailing_message_estimate(monkeypatch) -> None:
    state = {"session_id": "session-1", "_runtime_workspace": "/workspace"}
    first_messages = [
        republic.system("Base instructions"),
        republic.user("Start"),
    ]
    first_request = _request(first_messages)
    plugin.after_llm_call(
        first_request,
        LLMCallResult(
            run_id=first_request.run_id,
            text="Done",
            usage={"prompt_tokens": 80, "completion_tokens": 20},
        ),
        state,
    )
    assert isinstance(state[plugin.STATE_BASELINE_KEY], plugin._UsageBaseline)

    trailing = republic.user("Continue")
    next_request = _request(
        [
            *first_messages,
            republic.assistant("Done"),
            trailing,
        ]
    )
    estimated_messages: list[list[republic.Message]] = []

    def estimate(messages: list[republic.Message], *, encoding_name: str) -> int:
        del encoding_name
        estimated_messages.append(messages)
        return 7

    monkeypatch.setattr(plugin, "estimate_context_tokens", estimate)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 106
        ),
    )

    result = plugin.before_llm_call(next_request, state)

    assert result is not None
    assert estimated_messages == [[trailing]]
    assert "estimated input context is 107 tokens" in result.messages[0].text


def test_tool_call_output_is_part_of_real_usage_baseline(monkeypatch) -> None:
    state = {"session_id": "session-1"}
    first_messages = [republic.user("Inspect")]
    first_request = _request(first_messages)
    tool_call = republic.ToolCall(id="call-1", name="read", arguments='{"path":"a.py"}')
    plugin.after_llm_call(
        first_request,
        LLMCallResult(
            run_id=first_request.run_id,
            tool_calls=[asdict(tool_call)],
            usage={"total_tokens": 75},
        ),
        state,
    )

    trailing = republic.tool(tool_call, "contents")
    next_request = _request(
        [
            *first_messages,
            republic.assistant(tool_calls=[tool_call]),
            trailing,
        ]
    )
    estimated_messages: list[list[republic.Message]] = []

    def estimate(messages: list[republic.Message], *, encoding_name: str) -> int:
        del encoding_name
        estimated_messages.append(messages)
        return 5

    monkeypatch.setattr(plugin, "estimate_context_tokens", estimate)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 80
        ),
    )

    assert plugin.before_llm_call(next_request, state) is None
    assert estimated_messages == [[trailing]]


def test_changed_request_prefix_falls_back_to_full_estimate(monkeypatch) -> None:
    state = {"session_id": "session-1"}
    first_request = _request([republic.user("Start")])
    plugin.after_llm_call(
        first_request,
        LLMCallResult(
            run_id=first_request.run_id,
            text="Done",
            usage={"total_tokens": 100},
        ),
        state,
    )
    next_messages = [
        republic.system("Changed instructions"),
        republic.user("Continue"),
    ]
    next_request = _request(next_messages)
    estimated_messages: list[list[republic.Message]] = []

    def estimate(messages: list[republic.Message], *, encoding_name: str) -> int:
        del encoding_name
        estimated_messages.append(messages)
        return 10

    monkeypatch.setattr(plugin, "estimate_context_tokens", estimate)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 10
        ),
    )

    assert plugin.before_llm_call(next_request, state) is None
    assert estimated_messages == [next_messages]


def test_usage_baseline_is_scoped_to_turn_state(monkeypatch) -> None:
    first_state: dict[str, Any] = {}
    first_messages = [republic.user("Start")]
    first_request = _request(first_messages)
    plugin.after_llm_call(
        first_request,
        LLMCallResult(
            run_id=first_request.run_id,
            text="Done",
            usage={"total_tokens": 100},
        ),
        first_state,
    )
    next_messages = [
        *first_messages,
        republic.assistant("Done"),
        republic.user("Continue"),
    ]
    estimated_messages: list[list[republic.Message]] = []

    def estimate(messages: list[republic.Message], *, encoding_name: str) -> int:
        del encoding_name
        estimated_messages.append(messages)
        return 10

    monkeypatch.setattr(plugin, "estimate_context_tokens", estimate)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 10
        ),
    )

    assert plugin.before_llm_call(_request(next_messages), {}) is None
    assert estimated_messages == [next_messages]


def test_failed_call_does_not_replace_usage_baseline() -> None:
    state: dict[str, Any] = {}
    request = _request([republic.user("Start")])

    plugin.after_llm_call(
        request,
        LLMCallResult(
            run_id=request.run_id,
            usage={"total_tokens": 100},
            error=RuntimeError("failed"),
        ),
        state,
    )

    assert plugin.STATE_BASELINE_KEY not in state


def test_before_llm_call_leaves_request_at_budget(monkeypatch) -> None:
    messages = [republic.user("hello")]
    request = _request(messages)
    monkeypatch.setattr(plugin, "estimate_context_tokens", lambda *args, **kwargs: 50)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 50
        ),
    )

    result = plugin.before_llm_call(request, {})

    assert result is None
    assert request.messages == messages


def test_default_budget_triggers_above_reserved_threshold(monkeypatch) -> None:
    request = _request([republic.system("Base instructions")])
    threshold = plugin.DEFAULT_MAX_CONTEXT_TOKENS - plugin.RESERVE_TOKENS
    monkeypatch.setattr(
        plugin.bub, "ensure_config", lambda _: plugin.ContextBudgetSettings()
    )
    monkeypatch.setattr(
        plugin,
        "estimate_context_tokens",
        lambda *args, **kwargs: threshold,
    )

    assert plugin.before_llm_call(request, {}) is None

    monkeypatch.setattr(
        plugin,
        "estimate_context_tokens",
        lambda *args, **kwargs: threshold + 1,
    )

    assert plugin.before_llm_call(request, {}) is not None


def test_before_llm_call_appends_handoff_instruction_to_system_message(
    monkeypatch,
) -> None:
    messages = [
        republic.system("Base instructions"),
        republic.user("Continue the task"),
    ]
    request = _request(messages)
    monkeypatch.setattr(plugin, "estimate_context_tokens", lambda *args, **kwargs: 51)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 50
        ),
    )

    result = plugin.before_llm_call(request, {})

    assert result is not None
    assert result is not request
    assert result.messages is not request.messages
    assert result.messages[1] == messages[1]
    assert result.messages[0].text.startswith("Base instructions\n\n")
    assert "estimated input context is 51 tokens" in result.messages[0].text
    assert "threshold of 50 tokens" in result.messages[0].text
    assert f"with {plugin.RESERVE_TOKENS} tokens reserved" in result.messages[0].text
    assert "`tape.handoff`" in result.messages[0].text
    assert request.messages == messages


def test_before_llm_call_does_not_duplicate_handoff_instruction(monkeypatch) -> None:
    request = _request([republic.system("Base instructions")])
    monkeypatch.setattr(plugin, "estimate_context_tokens", lambda *args, **kwargs: 51)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 50
        ),
    )

    first = plugin.before_llm_call(request, {})

    assert first is not None
    assert plugin.before_llm_call(first, {}) is None
    assert first.messages[0].text.count(plugin.HANDOFF_INSTRUCTION_MARKER) == 1


def test_before_llm_call_prepends_system_message_when_none_exists(monkeypatch) -> None:
    messages = [republic.user("hello")]
    request = _request(messages)
    monkeypatch.setattr(plugin, "estimate_context_tokens", lambda *args, **kwargs: 101)
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda _: plugin.ContextBudgetSettings(
            max_context_tokens=plugin.RESERVE_TOKENS + 100
        ),
    )

    result = plugin.before_llm_call(request, {})

    assert result is not None
    assert result.messages[0].role == "system"
    assert result.messages[1:] == messages


def test_settings_reject_non_positive_budget() -> None:
    with pytest.raises(ValidationError):
        plugin.ContextBudgetSettings(max_context_tokens=0)


def test_settings_defaults_to_200k_budget() -> None:
    settings = plugin.ContextBudgetSettings()

    assert settings.max_context_tokens == 200_000
    assert plugin.RESERVE_TOKENS == 16_384


def test_settings_read_environment(monkeypatch) -> None:
    monkeypatch.setenv("BUB_CONTEXT_BUDGET_MAX_CONTEXT_TOKENS", "64000")
    monkeypatch.setenv("BUB_CONTEXT_BUDGET_ENCODING_NAME", "cl100k_base")

    settings = plugin.ContextBudgetSettings()

    assert settings.max_context_tokens == 64_000
    assert settings.encoding_name == "cl100k_base"
