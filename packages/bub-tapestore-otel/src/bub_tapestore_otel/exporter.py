from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from loguru import logger
from bub.tape import TapeEntry
from pydantic import BaseModel, ConfigDict, Field

FORCE_FLUSH_TIMEOUT_MS = 3_000
DEFAULT_AGENT_NAME = "bub"
TERMINAL_STEP_STATUSES = frozenset({"ok", "error", "failed", "cancelled"})
TRACER_NAME = "bub_tapestore_otel"


class TapeProjectionModel(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)


class OTelTapeExporterSettings(TapeProjectionModel):
    service_name: str = "bub"
    agent_name: str = DEFAULT_AGENT_NAME


class OTelExporterRuntime(TapeProjectionModel):
    provider: Any
    tracer: Any


class ToolCall(TapeProjectionModel):
    id: str
    name: str
    arguments: str
    result: str | None = None


class TraceMessage(TapeProjectionModel):
    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class TraceProjection(TapeProjectionModel):
    tape: str
    entries: list[TapeEntry]
    input_messages: list[TraceMessage]
    output_messages: list[TraceMessage]
    tool_calls: list[ToolCall]
    output: str | None = None
    provider: str | None = None
    model: str | None = None
    status: str | None = None
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    usage_total_tokens: int | None = None
    duration_ms: int | float | None = None


class StepTrace(TraceProjection):
    step: int
    llm_attributes: dict[str, Any] = Field(default_factory=dict)


class TapeTrace(TraceProjection):
    agent_name: str = DEFAULT_AGENT_NAME
    system_prompt: str | None = None
    prompt: str | None = None
    steps: list[StepTrace] = Field(default_factory=list)
    agent_attributes: dict[str, Any] = Field(default_factory=dict)
    llm_attributes: dict[str, Any] = Field(default_factory=dict)


class OTelTapeExporter:
    def __init__(self, settings: OTelTapeExporterSettings | None = None) -> None:
        self._settings = settings or OTelTapeExporterSettings()
        self._lock = threading.Lock()
        self._pending: dict[str, list[TapeEntry]] = {}
        self._runtime: OTelExporterRuntime | None = None

    def append(self, tape: str, entry: TapeEntry) -> None:
        try:
            self._append(tape, entry)
        except Exception:
            logger.opt(exception=True).warning("tapestore.otel.export_failed action=append tape={}", tape)

    def reset(self, tape: str) -> None:
        try:
            self._reset(tape)
        except Exception:
            logger.opt(exception=True).warning("tapestore.otel.export_failed action=reset tape={}", tape)

    def _ensure_exporter(self) -> OTelExporterRuntime:
        with self._lock:
            if self._runtime is None:
                self._runtime = _build_otel_exporter_runtime(self._settings.service_name)
            return self._runtime

    def _flush(self, runtime: OTelExporterRuntime) -> None:
        runtime.provider.force_flush(timeout_millis=FORCE_FLUSH_TIMEOUT_MS)

    def _append(self, tape: str, entry: TapeEntry) -> None:
        runtime = self._ensure_exporter()
        batch = self._record_entry(tape, entry)
        if batch is None:
            return
        _instrument_trace(build_tape_trace(tape, batch, agent_name=self._settings.agent_name), tracer=runtime.tracer)
        self._flush(runtime)

    def _reset(self, tape: str) -> None:
        runtime = self._ensure_exporter()
        batch = self._pop_pending(tape)
        if batch:
            _instrument_trace(build_tape_trace(tape, batch, agent_name=self._settings.agent_name), tracer=runtime.tracer)
        _instrument_reset(tape, tracer=runtime.tracer)
        self._flush(runtime)

    def _record_entry(self, tape: str, entry: TapeEntry) -> list[TapeEntry] | None:
        with self._lock:
            entries = self._pending.setdefault(tape, [])
            entries.append(entry)
            if not _should_flush_batch(entry):
                return None
            return self._pending.pop(tape)

    def _pop_pending(self, tape: str) -> list[TapeEntry]:
        with self._lock:
            return self._pending.pop(tape, [])


def _build_otel_exporter_runtime(service_name: str) -> OTelExporterRuntime:
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(_build_otel_span_processor())
    return OTelExporterRuntime(provider=provider, tracer=provider.get_tracer(TRACER_NAME))


def _build_otel_span_processor() -> object:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(OTLPSpanExporter())


def build_tape_trace(tape: str, entries: list[TapeEntry], *, agent_name: str = DEFAULT_AGENT_NAME) -> TapeTrace:
    steps = [_build_step_trace(tape, step, index) for index, step in enumerate(_split_step_entries(entries), start=1)]
    prompt_tokens, completion_tokens, total_tokens = _combined_usage(entries)
    fields = _trace_projection_fields(tape, entries)
    fields.update(
        agent_name=agent_name,
        system_prompt=_first_message_content(fields["input_messages"], "system"),
        prompt=_first_prompt(entries),
        usage_input_tokens=prompt_tokens,
        usage_output_tokens=completion_tokens,
        usage_total_tokens=total_tokens,
        duration_ms=_valid_duration_ms(_combined_duration_ms(entries)),
        steps=steps,
    )
    trace = TapeTrace(**fields)
    return _with_trace_attributes(trace)


def _build_step_trace(tape: str, entries: list[TapeEntry], index: int) -> StepTrace:
    step_data = _last_event_data(entries, "loop.step")
    step = StepTrace(
        **_trace_projection_fields(tape, entries),
        step=_step_number(step_data, index),
    )
    return step.model_copy(update={"llm_attributes": _llm_attributes(step) | _step_attributes(step)})


def _trace_projection_fields(tape: str, entries: list[TapeEntry]) -> dict[str, Any]:
    run_data = _last_event_data(entries, "run")
    step_data = _last_event_data(entries, "loop.step")
    prompt = _first_prompt(entries)
    messages, tool_calls = _extract_messages_and_tools(entries)
    input_messages, output_messages = _split_input_output(messages, prompt, tool_calls)
    output = _output_value(output_messages, tool_calls)
    prompt_tokens, completion_tokens, total_tokens = _usage(run_data)
    duration_ms = step_data.get("elapsed_ms") or run_data.get("elapsed_ms")
    return {
        "tape": tape,
        "entries": entries,
        "input_messages": input_messages,
        "output_messages": output_messages,
        "tool_calls": tool_calls,
        "output": output,
        "provider": _as_text(run_data.get("provider")),
        "model": _as_text(run_data.get("model")),
        "status": _as_text(step_data.get("status") or run_data.get("status")),
        "usage_input_tokens": prompt_tokens,
        "usage_output_tokens": completion_tokens,
        "usage_total_tokens": total_tokens,
        "duration_ms": _valid_duration_ms(duration_ms),
    }


def _with_trace_attributes(trace: TapeTrace) -> TapeTrace:
    agent_attributes = _genai_span_attributes(trace, operation_name="invoke_agent")
    if trace.agent_name:
        agent_attributes["gen_ai.agent.name"] = trace.agent_name
    agent_attributes.update(_bub_batch_attributes(trace))
    agent_attributes.update(_openinference_span_attributes(trace, span_kind="AGENT"))
    if trace.status:
        agent_attributes["bub.tape.status"] = trace.status

    return trace.model_copy(update={"agent_attributes": agent_attributes, "llm_attributes": _llm_attributes(trace)})


def _llm_attributes(projection: TraceProjection) -> dict[str, Any]:
    attributes = _genai_span_attributes(projection, operation_name="chat")
    attributes.update(_openinference_span_attributes(projection, span_kind="LLM"))
    attributes.update(_openinference_messages("llm.input_messages", projection.input_messages))
    attributes.update(_openinference_messages("llm.output_messages", projection.output_messages))
    return attributes


def _genai_span_attributes(projection: TraceProjection, *, operation_name: str) -> dict[str, Any]:
    attributes = _genai_conversation_attributes(projection.tape) | {
        "gen_ai.operation.name": operation_name,
    }
    if projection.model:
        attributes["gen_ai.request.model"] = projection.model
        attributes["gen_ai.response.model"] = projection.model
    if projection.provider:
        attributes["gen_ai.provider.name"] = projection.provider
    attributes.update(_genai_usage_attributes(projection))
    return attributes


def _genai_conversation_attributes(tape: str) -> dict[str, str]:
    return {"gen_ai.conversation.id": tape}


def _genai_usage_attributes(projection: TraceProjection) -> dict[str, int]:
    attributes = {
        "gen_ai.usage.input_tokens": projection.usage_input_tokens,
        "gen_ai.usage.output_tokens": projection.usage_output_tokens,
    }
    return {name: value for name, value in attributes.items() if value is not None}


def _openinference_span_attributes(projection: TraceProjection, *, span_kind: str) -> dict[str, Any]:
    attributes = {
        "openinference.span.kind": span_kind,
        "input.value": _messages_text(projection.input_messages),
        "output.value": projection.output or "",
    }
    if projection.model:
        attributes["llm.model_name"] = projection.model
    if projection.provider:
        attributes["llm.provider"] = projection.provider
    attributes.update(_openinference_usage_attributes(projection))
    return attributes


def _openinference_usage_attributes(projection: TraceProjection) -> dict[str, int]:
    attributes = {
        "llm.token_count.prompt": projection.usage_input_tokens,
        "llm.token_count.completion": projection.usage_output_tokens,
        "llm.token_count.total": projection.usage_total_tokens,
    }
    return {name: value for name, value in attributes.items() if value is not None}


def _step_attributes(step: StepTrace) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "bub.agent.step": step.step,
        "bub.tape.batch.entries": len(step.entries),
    }
    if step.status:
        attributes["bub.tape.status"] = step.status
    if step.duration_ms is not None:
        attributes["bub.agent.step.duration_ms"] = step.duration_ms
    return attributes


def _bub_batch_attributes(projection: TraceProjection) -> dict[str, int]:
    return {"bub.tape.batch.entries": len(projection.entries)}


def _split_step_entries(entries: list[TapeEntry]) -> list[list[TapeEntry]]:
    steps: list[list[TapeEntry]] = []
    current: list[TapeEntry] = []

    for entry in entries:
        current.append(entry)
        if entry.kind == "event" and _entry_name(entry) == "loop.step":
            steps.append(current)
            current = []

    if current and not steps:
        steps.append(current)
    return steps


def _extract_messages_and_tools(entries: list[TapeEntry]) -> tuple[list[TraceMessage], list[ToolCall]]:
    messages: list[TraceMessage] = []
    pending_calls: list[ToolCall] = []

    for entry in entries:
        if entry.kind == "system":
            content = _as_text(entry.payload.get("content"))
            if content:
                messages.append(TraceMessage(role="system", content=content))
        elif entry.kind == "message":
            message = _message_entry(entry)
            if message is not None:
                messages.append(message)
        elif entry.kind == "tool_call":
            calls = [_tool_call(call, index) for index, call in enumerate(_payload_list(entry, "calls"))]
            pending_calls.extend(calls)
            if calls:
                messages.append(TraceMessage(role="assistant", tool_calls=tuple(calls)))
        elif entry.kind == "tool_result":
            results = _payload_list(entry, "results")
            pending_calls = _attach_tool_results(pending_calls, results)
            for index, result in enumerate(results):
                tool_call = pending_calls[index] if index < len(pending_calls) else None
                messages.append(
                    TraceMessage(
                        role="tool",
                        content=_attribute_text(result),
                        tool_call_id=tool_call.id if tool_call else None,
                    )
                )

    return messages, pending_calls


def _message_entry(entry: TapeEntry) -> TraceMessage | None:
    role = _as_text(entry.payload.get("role")) or "assistant"
    content = _as_text(entry.payload.get("content")) or ""
    name = _as_text(entry.payload.get("name"))
    tool_call_id = _as_text(entry.payload.get("tool_call_id"))
    raw_tool_calls = entry.payload.get("tool_calls")
    tool_calls = ()
    if isinstance(raw_tool_calls, list):
        tool_calls = tuple(_tool_call(call, index) for index, call in enumerate(raw_tool_calls))
    if not content and not tool_calls:
        return None
    return TraceMessage(role=role, content=content, name=name, tool_call_id=tool_call_id, tool_calls=tool_calls)


def _split_input_output(
    messages: list[TraceMessage], prompt: str | None, tool_calls: list[ToolCall]
) -> tuple[list[TraceMessage], list[TraceMessage]]:
    if (last_assistant_index := _last_message_index(messages, _has_assistant_content)) is not None:
        return messages[:last_assistant_index], [messages[last_assistant_index]]

    if (last_tool_call_index := _last_message_index(messages, _has_assistant_tool_call)) is not None:
        return messages[:last_tool_call_index], [messages[last_tool_call_index]]

    if messages:
        return messages, []

    if prompt:
        return [TraceMessage(role="user", content=prompt)], []
    if tool_calls:
        return [], [TraceMessage(role="assistant", tool_calls=tuple(tool_calls))]
    return [], []


def _last_message_index(messages: list[TraceMessage], predicate: Callable[[TraceMessage], bool]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if predicate(messages[index]):
            return index
    return None


def _has_assistant_content(message: TraceMessage) -> bool:
    return message.role == "assistant" and bool(message.content)


def _has_assistant_tool_call(message: TraceMessage) -> bool:
    return message.role == "assistant" and bool(message.tool_calls)


def _tool_call(raw: Any, index: int) -> ToolCall:
    call = raw if isinstance(raw, dict) else {"value": raw}
    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name") or call.get("name") or f"tool_{index}"
        arguments = function.get("arguments") or call.get("arguments") or call.get("args") or {}
    else:
        name = call.get("name") or call.get("tool_name") or f"tool_{index}"
        arguments = call.get("arguments") or call.get("args") or call.get("input") or {}
    return ToolCall(
        id=str(call.get("id") or call.get("tool_call_id") or f"tool-{index}"),
        name=str(name),
        arguments=_attribute_text(arguments),
    )


def _attach_tool_results(tool_calls: list[ToolCall], results: list[Any]) -> list[ToolCall]:
    if not tool_calls:
        return []
    updated: list[ToolCall] = []
    for index, call in enumerate(tool_calls):
        result = _attribute_text(results[index]) if index < len(results) else call.result
        updated.append(ToolCall(id=call.id, name=call.name, arguments=call.arguments, result=result))
    return updated


def _bub_tape_attributes(tape: str, entries: list[TapeEntry]) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "bub.tape.name": tape,
        "bub.session.hash": _session_hash(tape),
    }
    if entries:
        attributes.update({
            "bub.tape.entry.first_id": entries[0].id,
            "bub.tape.entry.last_id": entries[-1].id,
            "bub.tape.entry.first_date": entries[0].date,
            "bub.tape.entry.last_date": entries[-1].date,
        })
    return attributes


def _common_attributes(tape: str, entries: list[TapeEntry]) -> dict[str, Any]:
    return _genai_conversation_attributes(tape) | _bub_tape_attributes(tape, entries)


def _openinference_messages(prefix: str, messages: list[TraceMessage]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    for index, message in enumerate(messages):
        base = f"{prefix}.{index}.message"
        attributes[f"{base}.role"] = message.role
        if message.content:
            attributes[f"{base}.content"] = message.content
        if message.name:
            attributes[f"{base}.name"] = message.name
        if message.tool_call_id:
            attributes[f"{base}.tool_call_id"] = message.tool_call_id
        for call_index, call in enumerate(message.tool_calls):
            call_base = f"{base}.tool_calls.{call_index}.tool_call"
            attributes[f"{call_base}.id"] = call.id
            attributes[f"{call_base}.function.name"] = call.name
            attributes[f"{call_base}.function.arguments"] = call.arguments
    return attributes


def _tool_span_attributes(step: StepTrace, call: ToolCall) -> dict[str, Any]:
    attributes = _genai_conversation_attributes(step.tape) | _bub_tape_attributes(step.tape, step.entries)
    attributes.update(_step_attributes(step))
    attributes.update({
        "openinference.span.kind": "TOOL",
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": call.name,
        "gen_ai.tool.call.id": call.id,
        "gen_ai.tool.type": "function",
        "gen_ai.tool.call.arguments": call.arguments,
        "bub.tool.name": call.name,
        "bub.tool.call.id": call.id,
        "input.mime_type": "application/json",
        "input.value": call.arguments,
        "output.value": call.result or "",
    })
    if call.result is not None:
        attributes["gen_ai.tool.call.result"] = call.result
        attributes["output.mime_type"] = "application/json"
    return attributes


def _payload_list(entry: TapeEntry, key: str) -> list[Any]:
    value = entry.payload.get(key)
    return value if isinstance(value, list) else []


def _messages_text(messages: list[TraceMessage]) -> str:
    return "\n".join(f"{message.role}: {message.content}" for message in messages if message.content)


def _first_message_content(messages: list[TraceMessage], role: str) -> str | None:
    for message in messages:
        if message.role == role and message.content:
            return message.content
    return None


def _output_value(messages: list[TraceMessage], tool_calls: list[ToolCall]) -> str | None:
    content = "\n".join(message.content for message in messages if message.content)
    if content:
        return content
    calls = [call for message in messages for call in message.tool_calls] or tool_calls
    if calls:
        return _compact_json([{"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls])
    return None


def _first_prompt(entries: list[TapeEntry]) -> str | None:
    for entry in entries:
        data = _payload_data(entry)
        prompt = data.get("prompt")
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, list):
            return _attribute_text(prompt)
    return None


def _last_event_data(entries: list[TapeEntry], name: str) -> dict[str, Any]:
    for entry in reversed(entries):
        if entry.kind == "event" and _entry_name(entry) == name:
            return _payload_data(entry)
    return {}


def _usage(data: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    return (
        _int_or_none(usage.get("prompt_tokens") or usage.get("input_tokens")),
        _int_or_none(usage.get("completion_tokens") or usage.get("output_tokens")),
        _int_or_none(usage.get("total_tokens")),
    )


def _combined_usage(entries: list[TapeEntry]) -> tuple[int | None, int | None, int | None]:
    totals = [0, 0, 0]
    saw_usage = False
    for entry in entries:
        if entry.kind != "event" or _entry_name(entry) != "run":
            continue
        usage = _usage(_payload_data(entry))
        if all(value is None for value in usage):
            continue
        saw_usage = True
        for index, value in enumerate(usage):
            if value is not None:
                totals[index] += value
    if not saw_usage:
        return None, None, None
    return totals[0], totals[1], totals[2]


def _combined_duration_ms(entries: list[TapeEntry]) -> int | float | None:
    total = 0
    saw_duration = False
    for entry in entries:
        if entry.kind != "event" or _entry_name(entry) != "loop.step":
            continue
        elapsed_ms = _payload_data(entry).get("elapsed_ms")
        if isinstance(elapsed_ms, (int, float)) and not isinstance(elapsed_ms, bool):
            total += elapsed_ms
            saw_duration = True
    return total if saw_duration else None


def _valid_duration_ms(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _step_number(data: dict[str, Any], fallback: int) -> int:
    value = data.get("step")
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _entry_name(entry: TapeEntry) -> str:
    value = entry.payload.get("name")
    return str(value) if value else entry.kind


def _payload_data(entry: TapeEntry) -> dict[str, Any]:
    data = entry.payload.get("data")
    return data if isinstance(data, dict) else {}


def _should_flush_batch(entry: TapeEntry) -> bool:
    if entry.kind != "event":
        return False
    if _entry_name(entry) == "command":
        return True
    if _entry_name(entry) != "loop.step":
        return False
    return _is_terminal_step(entry)


def _is_terminal_step(entry: TapeEntry) -> bool:
    status = _as_text(_payload_data(entry).get("status"))
    return status in TERMINAL_STEP_STATUSES


def _session_hash(tape: str) -> str:
    return hashlib.sha256(tape.encode("utf-8")).hexdigest()[:16]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _attribute_text(value)


def _attribute_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _compact_json(value)


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _instrument_trace(trace: TapeTrace, *, tracer: Any) -> None:
    from opentelemetry.trace import SpanKind

    with _otel_span(tracer, _agent_span_name(trace), kind=SpanKind.INTERNAL, attributes=trace.agent_attributes):
        for step in trace.steps:
            with _otel_span(tracer, "bub.agent.step", kind=SpanKind.INTERNAL, attributes=_step_span_attributes(step)):
                with _otel_span(tracer, _llm_span_name(step), kind=SpanKind.CLIENT, attributes=step.llm_attributes):
                    pass

                for call in step.tool_calls:
                    with _otel_span(
                        tracer,
                        _tool_span_name(call),
                        kind=SpanKind.INTERNAL,
                        attributes=_tool_span_attributes(step, call),
                    ):
                        pass


def _llm_span_name(step: StepTrace) -> str:
    return f"chat {step.model}" if step.model else "chat"


def _agent_span_name(trace: TapeTrace) -> str:
    return f"invoke_agent {trace.agent_name}" if trace.agent_name else "invoke_agent"


def _tool_span_name(call: ToolCall) -> str:
    return f"execute_tool {call.name or 'tool'}"


def _step_span_attributes(step: StepTrace) -> dict[str, Any]:
    return _common_attributes(step.tape, step.entries) | _step_attributes(step)


def _instrument_reset(tape: str, *, tracer: Any) -> None:
    from opentelemetry.trace import SpanKind

    with _otel_span(
        tracer,
        "bub.tape.reset",
        kind=SpanKind.INTERNAL,
        attributes={"bub.tape.name": tape, "bub.session.hash": _session_hash(tape)},
    ):
        pass


@contextmanager
def _otel_span(tracer: Any, name: str, *, kind: object, attributes: Mapping[str, Any]) -> Iterator[None]:
    with tracer.start_as_current_span(name, kind=kind, attributes=_otel_attributes(attributes)):
        yield


def _otel_attributes(attributes: Mapping[str, Any]) -> dict[str, str | bool | int | float]:
    return {name: value for name, value in attributes.items() if isinstance(value, (str, bool, int, float))}
