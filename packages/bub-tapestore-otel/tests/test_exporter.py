from __future__ import annotations
from contextlib import contextmanager
from types import SimpleNamespace

import bub_tapestore_otel.exporter as exporter
from bub_tapestore_otel.exporter import OTelTapeExporter, _instrument_trace, _should_flush_batch, build_tape_trace
from republic import TapeEntry


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
    assert trace.agent_attributes["gen_ai.provider.name"] == "openai"
    assert trace.agent_attributes["gen_ai.request.model"] == "gpt-5-mini"
    assert trace.agent_attributes["gen_ai.conversation.id"] == "chat__1"
    assert trace.agent_attributes["input.value"] == "system: system rules\nuser: say hello"
    assert trace.agent_attributes["output.value"] == "hello"

    assert trace.llm_attributes["openinference.span.kind"] == "LLM"
    assert trace.llm_attributes["gen_ai.operation.name"] == "chat"
    assert trace.llm_attributes["gen_ai.provider.name"] == "openai"
    assert trace.llm_attributes["gen_ai.request.model"] == "gpt-5-mini"
    assert trace.llm_attributes["gen_ai.usage.input_tokens"] == 11
    assert trace.llm_attributes["gen_ai.usage.output_tokens"] == 3
    assert trace.llm_attributes["llm.token_count.total"] == 14
    assert trace.llm_attributes["llm.input_messages.0.message.role"] == "system"
    assert trace.llm_attributes["llm.input_messages.0.message.content"] == "system rules"
    assert trace.llm_attributes["llm.input_messages.1.message.role"] == "user"
    assert trace.llm_attributes["llm.input_messages.1.message.content"] == "say hello"
    assert trace.llm_attributes["llm.output_messages.0.message.role"] == "assistant"
    assert trace.llm_attributes["llm.output_messages.0.message.content"] == "hello"
    assert "gen_ai.input.messages" not in trace.llm_attributes
    assert "gen_ai.output.messages" not in trace.llm_attributes


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
    assert trace.steps[0].tool_calls[0].name == "search"
    assert "llm.tools.0.tool.json_schema" not in trace.steps[0].llm_attributes


def test_build_tape_trace_groups_a_turn_into_steps() -> None:
    entries = [
        TapeEntry.event("loop.step.start", data={"step": 1, "prompt": "first"}),
        TapeEntry.message({"role": "user", "content": "first"}),
        TapeEntry.tool_call([{"id": "call_1", "name": "search", "arguments": {"query": "otel"}}]),
        TapeEntry.tool_result(["result"]),
        TapeEntry.event(
            "run",
            data={
                "provider": "openai",
                "model": "gpt-5-mini",
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        ),
        TapeEntry.event("loop.step", data={"step": 1, "status": "continue", "elapsed_ms": 100}),
        TapeEntry.event("loop.step.start", data={"step": 2, "prompt": "second"}),
        TapeEntry.message({"role": "assistant", "content": "done"}),
        TapeEntry.event(
            "run",
            data={
                "provider": "openai",
                "model": "gpt-5-mini",
                "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
            },
        ),
        TapeEntry.event("loop.step", data={"step": 2, "status": "ok", "elapsed_ms": 200}),
    ]

    trace = build_tape_trace("agent__steps", entries)

    assert trace.usage_input_tokens == 30
    assert trace.usage_output_tokens == 6
    assert trace.usage_total_tokens == 36
    assert trace.duration_ms == 300
    assert [step.step for step in trace.steps] == [1, 2]
    assert [step.status for step in trace.steps] == ["continue", "ok"]
    assert trace.steps[0].tool_calls[0].name == "search"
    assert trace.steps[1].output == "done"


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
    assert _should_flush_batch(TapeEntry.event("loop.step", data={"status": "error"}))
    assert _should_flush_batch(TapeEntry.event("command", data={}))
    assert not _should_flush_batch(TapeEntry.event("loop.step", data={"status": "continue"}))
    assert not _should_flush_batch(TapeEntry.event("loop.step.start", data={}))


def test_instrument_trace_nests_steps_and_tools_under_agent(monkeypatch) -> None:
    spans: list[tuple[str, str | None, dict]] = []
    stack: list[str] = []

    class FakeTracer:
        @contextmanager
        def start_as_current_span(self, name, **kwargs):
            spans.append((name, stack[-1] if stack else None, kwargs["attributes"]))
            stack.append(name)
            try:
                yield SimpleNamespace(get_span_context=lambda: object())
            finally:
                stack.pop()

    trace = build_tape_trace(
        "agent__nested",
        [
            TapeEntry.message({"role": "user", "content": "search docs"}),
            TapeEntry.tool_call([{"id": "call_1", "name": "search", "arguments": {"query": "otel"}}]),
            TapeEntry.tool_result(["result"]),
            TapeEntry.event("run", data={"provider": "openai", "model": "gpt-5-mini"}),
            TapeEntry.event("loop.step", data={"step": 1, "status": "ok"}),
        ],
    )

    _instrument_trace(trace, tracer=FakeTracer())

    assert spans == [
        ("invoke_agent", None, trace.agent_attributes),
        ("bub.agent.step", "invoke_agent", exporter._step_span_attributes(trace.steps[0])),
        ("chat gpt-5-mini", "bub.agent.step", trace.steps[0].llm_attributes),
        (
            "execute_tool search",
            "bub.agent.step",
            exporter._tool_span_attributes(trace.steps[0], trace.steps[0].tool_calls[0]),
        ),
    ]
    assert spans[1][2]["bub.agent.step"] == 1
    assert spans[1][2]["gen_ai.conversation.id"] == "agent__nested"
    assert spans[2][2]["bub.agent.step"] == 1
    assert spans[3][2]["gen_ai.tool.call.arguments"] == '{"query":"otel"}'
    assert spans[3][2]["gen_ai.tool.call.result"] == "result"
    assert spans[3][2]["bub.tool.name"] == "search"


def test_exporter_uses_span_processor_without_shutdown(monkeypatch) -> None:
    calls: list[str] = []

    class FakeProvider:
        def force_flush(self, *, timeout_millis: int) -> None:
            calls.append(f"force_flush:{timeout_millis}")

    fake_runtime = exporter.OTelExporterRuntime(provider=FakeProvider(), tracer=object())

    monkeypatch.setattr(exporter, "_build_otel_exporter_runtime", lambda _service_name: calls.append("build_runtime") or fake_runtime)
    monkeypatch.setattr(
        exporter,
        "_instrument_trace",
        lambda _trace, *, tracer: calls.append(f"instrument_trace:{tracer is fake_runtime.tracer}"),
    )

    tape_exporter = OTelTapeExporter()
    tape_exporter.append("tape-1", TapeEntry.message({"role": "user", "content": "hello"}))
    tape_exporter.append("tape-1", TapeEntry.event("loop.step", data={"status": "ok"}))
    tape_exporter.append("tape-2", TapeEntry.event("command", data={}))

    assert calls == [
        "build_runtime",
        "instrument_trace:True",
        "force_flush:3000",
        "instrument_trace:True",
        "force_flush:3000",
    ]
