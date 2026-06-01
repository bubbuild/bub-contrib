from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger
from republic import TapeEntry

SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
SEND_TO_LOGFIRE = False
FORCE_FLUSH_TIMEOUT_MS = 3_000
SHUTDOWN_TIMEOUT_MS = 1_000


@dataclass(frozen=True)
class LogfireTapeExporterSettings:
    service_name: str = "bub"


@dataclass(frozen=True)
class TraceMessage:
    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple["ToolCall", ...] = ()


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    result: str | None = None


@dataclass(frozen=True)
class TapeTrace:
    tape: str
    entries: list[TapeEntry]
    input_messages: list[TraceMessage]
    output_messages: list[TraceMessage]
    tool_calls: list[ToolCall]
    system_prompt: str | None = None
    prompt: str | None = None
    output: str | None = None
    provider: str | None = None
    model: str | None = None
    status: str | None = None
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    usage_total_tokens: int | None = None
    duration_ms: int | float | None = None
    agent_attributes: dict[str, Any] = field(default_factory=dict)
    llm_attributes: dict[str, Any] = field(default_factory=dict)


class LogfireTapeExporter:
    def __init__(self, settings: LogfireTapeExporterSettings | None = None) -> None:
        self._settings = settings or LogfireTapeExporterSettings()
        self._configured = False
        self._lock = threading.Lock()
        self._pending: dict[str, list[TapeEntry]] = {}

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

    def _configure(self) -> None:
        if self._configured:
            return
        import logfire

        logfire.configure(
            send_to_logfire=SEND_TO_LOGFIRE,
            service_name=self._settings.service_name,
            console=False,
            scrubbing=False,
        )
        self._configured = True

    def _flush(self) -> None:
        import logfire

        logfire.force_flush(timeout_millis=FORCE_FLUSH_TIMEOUT_MS)
        logfire.shutdown(timeout_millis=SHUTDOWN_TIMEOUT_MS, flush=False)
        self._configured = False

    def _append(self, tape: str, entry: TapeEntry) -> None:
        self._configure()
        batch = self._record_entry(tape, entry)
        if batch is None:
            return
        _instrument_trace(build_tape_trace(tape, batch))
        self._flush()

    def _reset(self, tape: str) -> None:
        self._configure()
        batch = self._pop_pending(tape)
        if batch:
            _instrument_trace(build_tape_trace(tape, batch))
        _instrument_reset(tape)
        self._flush()

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


def build_tape_trace(tape: str, entries: list[TapeEntry]) -> TapeTrace:
    run_data = _last_event_data(entries, "run")
    step_data = _last_event_data(entries, "loop.step")
    prompt = _first_prompt(entries)
    messages, tool_calls = _extract_messages_and_tools(entries)
    input_messages, output_messages = _split_input_output(messages, prompt, tool_calls)
    system_prompt = _first_message_content(input_messages, "system")
    output = _output_value(output_messages, tool_calls)
    provider = _as_text(run_data.get("provider"))
    model = _as_text(run_data.get("model"))
    prompt_tokens, completion_tokens, total_tokens = _usage(run_data)
    status = _as_text(step_data.get("status") or run_data.get("status"))
    duration_ms = step_data.get("elapsed_ms") or run_data.get("elapsed_ms")

    trace = TapeTrace(
        tape=tape,
        entries=entries,
        input_messages=input_messages,
        output_messages=output_messages,
        tool_calls=tool_calls,
        system_prompt=system_prompt,
        prompt=prompt,
        output=output,
        provider=provider,
        model=model,
        status=status,
        usage_input_tokens=prompt_tokens,
        usage_output_tokens=completion_tokens,
        usage_total_tokens=total_tokens,
        duration_ms=duration_ms if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) else None,
    )
    return _with_trace_attributes(trace)


def _with_trace_attributes(trace: TapeTrace) -> TapeTrace:
    agent_attributes = _common_attributes(trace) | {
        "openinference.span.kind": "AGENT",
        "gen_ai.operation.name": "invoke_agent",
        "input.mime_type": "application/json",
        "input.value": _json_dumps(_message_payloads(trace.input_messages)),
        "output.mime_type": "application/json",
        "output.value": trace.output or "",
        "bub.tape.batch.entries": len(trace.entries),
    }
    if trace.status:
        agent_attributes["bub.tape.status"] = trace.status

    llm_attributes = _common_attributes(trace) | {
        "openinference.span.kind": "LLM",
        "gen_ai.operation.name": "chat",
        "gen_ai.input.messages": _json_dumps(_otel_messages(trace.input_messages)),
        "gen_ai.output.messages": _json_dumps(_otel_messages(trace.output_messages)),
        "input.mime_type": "application/json",
        "input.value": _json_dumps(_message_payloads(trace.input_messages)),
        "output.mime_type": "application/json",
        "output.value": trace.output or "",
        "gen_ai.output": trace.output or "",
    }
    if trace.model:
        llm_attributes["gen_ai.request.model"] = trace.model
        llm_attributes["gen_ai.response.model"] = trace.model
        llm_attributes["llm.model_name"] = trace.model
    if trace.provider:
        llm_attributes["gen_ai.provider.name"] = trace.provider
        llm_attributes["llm.provider"] = trace.provider
    if trace.usage_input_tokens is not None:
        llm_attributes["gen_ai.usage.input_tokens"] = trace.usage_input_tokens
        llm_attributes["llm.token_count.prompt"] = trace.usage_input_tokens
    if trace.usage_output_tokens is not None:
        llm_attributes["gen_ai.usage.output_tokens"] = trace.usage_output_tokens
        llm_attributes["llm.token_count.completion"] = trace.usage_output_tokens
    if trace.usage_total_tokens is not None:
        llm_attributes["llm.token_count.total"] = trace.usage_total_tokens
    if trace.duration_ms is not None:
        llm_attributes["gen_ai.server.time_to_last_token"] = trace.duration_ms / 1000
    llm_attributes.update(_openinference_messages("llm.input_messages", trace.input_messages))
    llm_attributes.update(_openinference_messages("llm.output_messages", trace.output_messages))
    llm_attributes.update(_openinference_tool_definitions(trace.tool_calls))

    return replace(trace, agent_attributes=agent_attributes, llm_attributes=llm_attributes)


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
                        content=_stringify(result),
                        tool_call_id=tool_call.id if tool_call else None,
                    )
                )

    return messages, pending_calls


def _message_entry(entry: TapeEntry) -> TraceMessage | None:
    role = _as_text(entry.payload.get("role")) or "assistant"
    content = _as_text(entry.payload.get("content")) or _stringify(entry.payload)
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
    last_assistant_index = _last_assistant_content_index(messages)
    if last_assistant_index is not None:
        return messages[:last_assistant_index], [messages[last_assistant_index]]

    last_tool_call_index = _last_tool_call_index(messages)
    if last_tool_call_index is not None:
        return messages[:last_tool_call_index], [messages[last_tool_call_index]]

    if messages:
        return messages, []

    if prompt:
        return [TraceMessage(role="user", content=prompt)], []
    if tool_calls:
        return [], [TraceMessage(role="assistant", tool_calls=tuple(tool_calls))]
    return [], []


def _last_assistant_content_index(messages: list[TraceMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and message.content:
            return index
    return None


def _last_tool_call_index(messages: list[TraceMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            return index
    return None


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
        arguments=_stringify(arguments),
    )


def _attach_tool_results(tool_calls: list[ToolCall], results: list[Any]) -> list[ToolCall]:
    if not tool_calls:
        return []
    updated: list[ToolCall] = []
    for index, call in enumerate(tool_calls):
        result = _stringify(results[index]) if index < len(results) else call.result
        updated.append(ToolCall(id=call.id, name=call.name, arguments=call.arguments, result=result))
    return updated


def _common_attributes(trace: TapeTrace) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "bub.tape.name": trace.tape,
        "bub.session.hash": _session_hash(trace.tape),
    }
    if trace.entries:
        attributes.update(
            {
                "bub.tape.entry.first_id": trace.entries[0].id,
                "bub.tape.entry.last_id": trace.entries[-1].id,
                "bub.tape.entry.first_date": trace.entries[0].date,
                "bub.tape.entry.last_date": trace.entries[-1].date,
            }
        )
    return attributes


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


def _openinference_tool_definitions(tool_calls: list[ToolCall]) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    seen: set[str] = set()
    for index, call in enumerate(tool_calls):
        if call.name in seen:
            continue
        seen.add(call.name)
        attributes[f"llm.tools.{index}.tool.json_schema"] = _json_dumps(
            {
                "type": "function",
                "function": {
                    "name": call.name,
                    "parameters": {"type": "object"},
                },
            }
        )
    return attributes


def _tool_span_attributes(trace: TapeTrace, call: ToolCall) -> dict[str, Any]:
    attributes = _common_attributes(trace) | {
        "openinference.span.kind": "TOOL",
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": call.name,
        "gen_ai.tool.args": call.arguments,
        "tool.name": call.name,
        "tool.call.id": call.id,
        "input.mime_type": "application/json",
        "input.value": call.arguments,
        "output.value": call.result or "",
    }
    if call.result is not None:
        attributes["gen_ai.output"] = call.result
        attributes["output.mime_type"] = "application/json"
    return attributes


def _otel_messages(messages: list[TraceMessage]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for message in messages:
        payload: dict[str, Any] = {"role": message.role}
        if message.content:
            payload["parts"] = [{"type": "text", "content": message.content}]
            payload["content"] = message.content
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
        payloads.append(payload)
    return payloads


def _message_payloads(messages: list[TraceMessage]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for message in messages:
        payload: dict[str, Any] = {"role": message.role}
        if message.content:
            payload["content"] = message.content
        if message.name:
            payload["name"] = message.name
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    **({"result": call.result} if call.result is not None else {}),
                }
                for call in message.tool_calls
            ]
        payloads.append(payload)
    return payloads


def _payload_list(entry: TapeEntry, key: str) -> list[Any]:
    value = entry.payload.get(key)
    return value if isinstance(value, list) else []


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
        return _json_dumps([{"id": call.id, "name": call.name, "arguments": call.arguments} for call in calls])
    return None


def _first_prompt(entries: list[TapeEntry]) -> str | None:
    for entry in entries:
        data = _payload_data(entry)
        prompt = data.get("prompt")
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, list):
            return _stringify(prompt)
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
    if entry.kind == "event" and _entry_name(entry) in {"command", "loop.step"}:
        return True
    return False


def _session_hash(tape: str) -> str:
    return hashlib.sha256(tape.encode("utf-8")).hexdigest()[:16]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _stringify(value)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return _json_dumps(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _instrument_trace(trace: TapeTrace) -> None:
    import logfire
    from opentelemetry.trace import SpanKind

    with logfire.span(
        "bub invoke_agent {tape}",
        _span_name="bub.invoke_agent",
        _span_kind=SpanKind.INTERNAL,
        tape=trace.tape,
        **trace.agent_attributes,
    ):
        llm_context = None
        with logfire.span(
            "bub chat {model}",
            _span_name="bub.llm.chat",
            _span_kind=SpanKind.CLIENT,
            model=trace.model or "unknown",
            **trace.llm_attributes,
        ) as llm_span:
            llm_context = llm_span.get_span_context()

        links = []
        if llm_context is not None:
            links.append((llm_context, {"bub.link.type": "llm_tool_call"}))
        for call in trace.tool_calls:
            with logfire.span(
                "bub tool {tool}",
                _span_name=f"bub.tool.{SAFE_NAME_RE.sub('.', call.name).strip('.') or 'call'}",
                _span_kind=SpanKind.CLIENT,
                _links=links,
                tool=call.name,
                **_tool_span_attributes(trace, call),
            ):
                pass


def _instrument_reset(tape: str) -> None:
    import logfire

    with logfire.span(
        "bub.tape.reset {tape}",
        _span_name="bub.tape.reset",
        **{"bub.tape.name": tape, "bub.session.hash": _session_hash(tape)},
    ):
        pass
