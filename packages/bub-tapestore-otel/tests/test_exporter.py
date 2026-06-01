from __future__ import annotations

import json

from republic import TapeEntry

from bub_tapestore_otel.exporter import _should_flush_batch, build_tape_trace


def test_build_tape_trace_exports_genai_and_openinference_llm_attributes() -> None:
    entries = [
        TapeEntry.system("system rules"),
        TapeEntry.message({"role": "user", "content": "say hello"}),
        TapeEntry.message({"role": "assistant", "content": "hello"}),
        TapeEntry.event(
            "run",
            data={
                "provider": "openai",
                "model": "gpt-5-mini",
                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
            },
        ),
        TapeEntry.event("loop.step", data={"status": "ok", "elapsed_ms": 125}),
    ]

    trace = build_tape_trace("chat__1", entries)

    assert trace.agent_attributes["openinference.span.kind"] == "AGENT"
    assert trace.agent_attributes["gen_ai.operation.name"] == "invoke_agent"
    assert trace.agent_attributes["output.value"] == "hello"

    assert trace.llm_attributes["openinference.span.kind"] == "LLM"
    assert trace.llm_attributes["gen_ai.operation.name"] == "chat"
    assert trace.llm_attributes["gen_ai.provider.name"] == "openai"
    assert trace.llm_attributes["gen_ai.request.model"] == "gpt-5-mini"
    assert trace.llm_attributes["gen_ai.output"] == "hello"
    assert trace.llm_attributes["gen_ai.usage.input_tokens"] == 11
    assert trace.llm_attributes["gen_ai.usage.output_tokens"] == 3
    assert trace.llm_attributes["llm.token_count.total"] == 14
    assert trace.llm_attributes["llm.input_messages.0.message.role"] == "system"
    assert trace.llm_attributes["llm.input_messages.0.message.content"] == "system rules"
    assert trace.llm_attributes["llm.input_messages.1.message.role"] == "user"
    assert trace.llm_attributes["llm.input_messages.1.message.content"] == "say hello"
    assert trace.llm_attributes["llm.output_messages.0.message.role"] == "assistant"
    assert trace.llm_attributes["llm.output_messages.0.message.content"] == "hello"

    input_messages = json.loads(trace.llm_attributes["gen_ai.input.messages"])
    output_messages = json.loads(trace.llm_attributes["gen_ai.output.messages"])
    assert input_messages == [
        {"role": "system", "parts": [{"type": "text", "content": "system rules"}], "content": "system rules"},
        {"role": "user", "parts": [{"type": "text", "content": "say hello"}], "content": "say hello"},
    ]
    assert output_messages == [
        {"role": "assistant", "parts": [{"type": "text", "content": "hello"}], "content": "hello"}
    ]


def test_build_tape_trace_exports_tool_calls_and_results() -> None:
    entries = [
        TapeEntry.message({"role": "user", "content": "search docs"}),
        TapeEntry.tool_call([{"id": "call_1", "name": "search", "arguments": {"query": "otel genai"}}]),
        TapeEntry.tool_result([{"title": "OpenTelemetry GenAI"}]),
        TapeEntry.event("loop.step", data={"status": "ok"}),
    ]

    trace = build_tape_trace("agent__tools", entries)

    assert trace.tool_calls[0].id == "call_1"
    assert trace.tool_calls[0].name == "search"
    assert trace.tool_calls[0].arguments == '{"query":"otel genai"}'
    assert trace.tool_calls[0].result == '{"title":"OpenTelemetry GenAI"}'
    assert trace.llm_attributes["llm.output_messages.0.message.tool_calls.0.tool_call.id"] == "call_1"
    assert trace.llm_attributes["llm.output_messages.0.message.tool_calls.0.tool_call.function.name"] == "search"
    assert (
        trace.llm_attributes["llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments"]
        == '{"query":"otel genai"}'
    )
    assert json.loads(trace.llm_attributes["llm.tools.0.tool.json_schema"]) == {
        "type": "function",
        "function": {"name": "search", "parameters": {"type": "object"}},
    }


def test_build_tape_trace_falls_back_to_prompt_when_messages_are_missing() -> None:
    trace = build_tape_trace(
        "prompt__1",
        [
            TapeEntry.event("loop.step.start", data={"prompt": "plain prompt"}),
            TapeEntry.event("loop.step", data={"status": "ok"}),
        ],
    )

    assert trace.input_messages[0].role == "user"
    assert trace.input_messages[0].content == "plain prompt"
    assert trace.llm_attributes["llm.input_messages.0.message.content"] == "plain prompt"


def test_batch_flushes_on_completed_tape_turn_markers() -> None:
    assert _should_flush_batch(TapeEntry.event("loop.step", data={"status": "ok"}))
    assert _should_flush_batch(TapeEntry.event("command", data={}))
    assert not _should_flush_batch(TapeEntry.event("loop.step.start", data={}))
