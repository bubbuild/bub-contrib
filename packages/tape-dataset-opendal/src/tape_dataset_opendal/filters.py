from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from celpy import CELParseError, CELEvalError, Environment, json_to_cel
from celpy.celtypes import BoolType
from republic.tape.entries import TapeEntry


def entry_text(entry: TapeEntry) -> str:
    fragments = list(_iter_text_fragments(entry.payload))
    return "\n".join(part for part in fragments if part)


def _iter_text_fragments(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str | int | float | bool):
        yield str(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_text_fragments(item)
        return
    if isinstance(value, list | tuple):
        for item in value:
            yield from _iter_text_fragments(item)


@dataclass(frozen=True)
class EntryFilterContext:
    tape: str
    entry: TapeEntry

    def to_mapping(self) -> dict[str, Any]:
        payload = dict(self.entry.payload)
        meta = dict(self.entry.meta)
        record = {
            "id": self.entry.id,
            "kind": self.entry.kind,
            "payload": payload,
            "meta": meta,
            "date": self.entry.date,
        }
        return {
            "tape": self.tape,
            "kind": self.entry.kind,
            "date": self.entry.date,
            "payload": payload,
            "meta": meta,
            "text": entry_text(self.entry),
            "json": json.dumps(record, sort_keys=True, ensure_ascii=False),
            "entry": record,
        }


class EntryFilter:
    def __init__(self, expressions: Sequence[str] | None = None) -> None:
        normalized = [expression.strip() for expression in expressions or [] if expression.strip()]
        self._expressions = tuple(normalized)
        self._programs = tuple(self._compile(expression) for expression in normalized)

    @property
    def expressions(self) -> tuple[str, ...]:
        return self._expressions

    def is_empty(self) -> bool:
        return not self._programs

    def matches(self, tape: str, entry: TapeEntry) -> bool:
        if not self._programs:
            return True
        activation = json_to_cel(EntryFilterContext(tape=tape, entry=entry).to_mapping())
        for expression, program in zip(self._expressions, self._programs, strict=True):
            try:
                result = program.evaluate(activation)
            except CELEvalError as exc:
                raise ValueError(f"Failed to evaluate CEL filter '{expression}': {exc}") from exc
            if not isinstance(result, bool | BoolType):
                raise ValueError(
                    f"CEL filter '{expression}' must evaluate to bool, got {type(result).__name__}."
                )
            if not bool(result):
                return False
        return True

    @staticmethod
    def _compile(expression: str):
        environment = Environment()
        try:
            ast = environment.compile(expression)
        except CELParseError as exc:
            raise ValueError(f"Invalid CEL filter '{expression}': {exc}") from exc
        return environment.program(ast)
