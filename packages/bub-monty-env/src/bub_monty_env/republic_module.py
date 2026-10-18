"""The ``republic`` module of ``run_code`` in Monty, served by Bub as in its subprocess runner.

Bub's subprocess runner gives the code a ``republic`` module whose models call back into Bub
as tool calls named ``republic.<operation>``, with Republic values as JSON objects carrying a
``"$republic"`` key (see ``runner_child`` in Bub's code mode). Bub's ``call_tool`` serves those
calls with its own credentials and settings. This module builds the same API from host objects
that Monty exposes to the code, and makes the same calls, so Bub's side is unchanged.

Two differences follow from Monty:

- Monty has no import hook, so ``republic`` is a preset global and
  :func:`rewrite_republic_imports` turns ``import republic`` statements into assignments from it.
- Monty has no ``async with`` or ``async for``, so ``model.stream()`` is not available and
  models are used without ``async with``; ``await model.chat(...)`` returns the whole response.
"""

from __future__ import annotations

import ast
import base64
import types
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic_monty import ClassInstance, ClassType

type Call = Callable[[str, dict[str, Any]], Awaitable[Any]]

REPUBLIC_KEY = "$republic"
STREAM_UNSUPPORTED = (
    "model.stream() needs `async with` and `async for`, which Monty does not support;"
    " use `await model.chat(...)` for the whole response."
)


class _Media:
    kind = "media"

    def __init__(
        self, media_type: str, data: bytes | None = None, url: str | None = None
    ) -> None:
        if data is None and url is None:
            raise ValueError(f"{type(self).__name__} needs data or a url")
        self.media_type = media_type
        self.data = data
        self.url = url

    @property
    def base64_data(self) -> str:
        if self.data is None:
            raise ValueError(f"This {self.kind} has no inline data")
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        if self.url is not None:
            return self.url
        return f"data:{self.media_type};base64,{self.base64_data}"

    def to_json(self) -> dict[str, Any]:
        data = None if self.data is None else self.base64_data
        return {
            REPUBLIC_KEY: self.kind,
            "media_type": self.media_type,
            "data": data,
            "url": self.url,
        }


class Image(_Media):
    kind = "image"


class Audio(_Media):
    kind = "audio"


class Video(_Media):
    kind = "video"


class Message:
    """A chat message whose content mixes text and media."""

    def __init__(self, role: str, content: list[Any]) -> None:
        self.role = role
        self.content = list(content)

    @property
    def text(self) -> str:
        return "".join(item for item in self.content if isinstance(item, str))

    def to_json(self) -> dict[str, Any]:
        return {
            REPUBLIC_KEY: "message",
            "role": self.role,
            "content": to_json(self.content),
        }


class _Question:
    kind = "question"

    def __init__(self, instructions: Any, criteria: Any = None) -> None:
        self.instructions = instructions
        self.criteria = criteria

    def to_json(self) -> dict[str, Any]:
        return {
            REPUBLIC_KEY: self.kind,
            "instructions": to_json(self.instructions),
            "criteria": to_json(self.criteria),
        }


class Noul(_Question):
    """A yes/no question answered with the probability of yes."""

    kind = "noul"


class Choice(_Question):
    """Pick one option; ``criteria`` maps options to descriptions or lists option names."""

    kind = "choice"

    def __init__(self, instructions: Any, criteria: Any) -> None:
        super().__init__(instructions, criteria)


class Score(_Question):
    """Rate along ordered levels, from lowest to highest."""

    kind = "score"

    def __init__(self, instructions: Any, criteria: Any) -> None:
        super().__init__(instructions, criteria)


class Record(types.SimpleNamespace):
    """A result whose fields are attributes."""


class _Model:
    def __init__(self, call: Call, spec: str | None) -> None:
        self._call = call
        self.spec = spec


class ChatModel(_Model):
    """A chat model served by Bub."""

    async def chat(self, prompt: Any, **options: Any) -> Any:
        """Send the conversation and wait for the complete response."""
        if isinstance(prompt, str | Message):
            prompt = [prompt]
        messages = [user(item) if isinstance(item, str) else item for item in prompt]
        arguments = {"spec": self.spec, "messages": messages, "options": options}
        return _record(await self._call("republic.chat", arguments))

    def stream(self, prompt: Any, **options: Any) -> Any:
        raise NotImplementedError(STREAM_UNSUPPORTED)


class EmbeddingModel(_Model):
    """An embedding model served by Bub."""

    async def embed(self, text: str, *, dimensions: int | None = None) -> Any:
        """Embed one text; the vector is ``response.vector``."""
        return await self.embed_many([text], dimensions=dimensions)

    async def embed_many(
        self, texts: list[str], *, dimensions: int | None = None
    ) -> Any:
        """Embed several texts in one request; ``response.vectors`` follows input order."""
        arguments = {"spec": self.spec, "texts": list(texts), "dimensions": dimensions}
        result = await self._call("republic.embed", arguments)
        if result.get("vectors"):
            result = {**result, "vector": result["vectors"][0]}
        return _record(result)


class DecisionModel(_Model):
    """A decision model served by Bub."""

    async def decide(self, state: Any, *, questions: dict[str, Any]) -> Any:
        """Answer every question about ``state``. Answers come back under the same ids."""
        arguments = {"spec": self.spec, "state": state, "questions": questions}
        result = await self._call("republic.decide", arguments)
        answers = {key: _record(answer) for key, answer in result["answers"].items()}
        fields = {
            key: _record(item) for key, item in result.items() if key != "answers"
        }
        # As in Bub's runner, `response.<id>` is a shortcut for `response.answers[<id>]`.
        return Record(**{**answers, **fields, "answers": answers})


def system(text: str) -> Message:
    return Message("system", [text])


def user(*content: Any) -> Message:
    return Message("user", list(content))


def assistant(*content: Any) -> Message:
    return Message("assistant", list(content))


class _SandboxType(ClassType):
    """A class for the code whose instances' attributes and results are wrapped for Monty."""

    def convert_value(self, /, name: str, value: Any) -> Any:
        return to_sandbox(value)


class _SandboxInstance(ClassInstance):
    """An object for the code whose attributes and method results are wrapped for Monty."""

    def convert_value(self, /, name: str, value: Any) -> Any:
        return to_sandbox(value)


def _class_type(cls: type[Any], **policies: Any) -> ClassType:
    return _SandboxType(cls, init=True, **policies)


_MEDIA_TYPES = {
    cls: _class_type(
        cls,
        instance_eager_attrs=("media_type", "data", "url"),
        instance_lazy_attrs={"base64_data", "data_url"},
    )
    for cls in (Image, Audio, Video)
}
_MEDIA_CLASSES: dict[str, type[_Media]] = {
    "image": Image,
    "audio": Audio,
    "video": Video,
}
_MESSAGE_TYPE = _class_type(
    Message, instance_eager_attrs=("role", "content"), instance_lazy_attrs={"text"}
)
_QUESTION_TYPES = {
    cls: _class_type(cls, instance_eager_attrs=("instructions", "criteria"))
    for cls in (Noul, Choice, Score)
}
_MODEL_TYPES = {
    ChatModel: _SandboxType(
        ChatModel,
        instance_eager_attrs=("spec",),
        instance_allowed_methods={"chat", "stream"},
    ),
    EmbeddingModel: _SandboxType(
        EmbeddingModel,
        instance_eager_attrs=("spec",),
        instance_allowed_methods={"embed", "embed_many"},
    ),
    DecisionModel: _SandboxType(
        DecisionModel,
        instance_eager_attrs=("spec",),
        instance_allowed_methods={"decide"},
    ),
}


def to_json(value: Any) -> Any:
    """Turn values from the code into JSON-compatible data with ``"$republic"`` objects."""
    if isinstance(value, _Media | Message | _Question):
        return value.to_json()
    if isinstance(value, Record):
        return {key: to_json(item) for key, item in vars(value).items()}
    if isinstance(value, Mapping):
        return {key: to_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_json(item) for item in value]
    return value


def from_json(value: Any) -> Any:
    """Turn ``"$republic"`` objects in data from Bub into Republic values for the code."""
    if isinstance(value, list):
        return [from_json(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(REPUBLIC_KEY)
    if kind in _MEDIA_CLASSES:
        data = value.get("data")
        return _MEDIA_CLASSES[kind](
            value["media_type"],
            data=None if data is None else base64.b64decode(data),
            url=value.get("url"),
        )
    if kind == "message":
        return Message(value["role"], from_json(value.get("content") or []))
    return {key: from_json(item) for key, item in value.items()}


def _record(value: Any) -> Any:
    """Like Bub's runner: dicts in a model response become records, recursively."""
    value = from_json(value)
    if isinstance(value, dict):
        return Record(**{key: _record(item) for key, item in value.items()})
    return value


def to_sandbox(value: Any) -> Any:
    """Wrap the objects in ``value`` for Monty, which only passes plain data by itself."""
    if isinstance(value, ClassInstance | ClassType):
        return value
    if isinstance(value, Message):
        return _MESSAGE_TYPE.instance_wrapper(value)
    if (cls := type(value)) in _MEDIA_TYPES:
        return _MEDIA_TYPES[cls].instance_wrapper(value)
    if cls in _QUESTION_TYPES:
        return _QUESTION_TYPES[cls].instance_wrapper(value)
    if cls in _MODEL_TYPES:
        return _MODEL_TYPES[cls].instance_wrapper(value)
    if isinstance(value, Record):
        return _SandboxInstance(value, eager_attrs="all")
    if isinstance(value, dict):
        return {key: to_sandbox(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_sandbox(item) for item in value]
    return value


_DECISIONS = ClassInstance(
    types.SimpleNamespace(**{cls.__name__: t for cls, t in _QUESTION_TYPES.items()}),
    eager_attrs="all",
)


def republic_module(
    call_tool: Call,
) -> tuple[ClassInstance, dict[str, Callable[..., Any]]]:
    """The ``republic`` global of one run, and the functions it references by name.

    Monty passes host functions as references to their ``__name__``, which
    ``external_lookup`` must resolve, so pass the functions there. Their names
    (``republic.<name>``) cannot clash with names in the code.
    """

    async def call(name: str, arguments: dict[str, Any]) -> Any:
        return from_json(await call_tool(name, to_json(arguments)))

    def get_model(spec: str | None = None) -> Any:
        return to_sandbox(ChatModel(call, spec))

    def get_embedding_model(spec: str) -> Any:
        return to_sandbox(EmbeddingModel(call, spec))

    def get_decision_model(spec: str) -> Any:
        return to_sandbox(DecisionModel(call, spec))

    def message(builder: Callable[..., Message]) -> Callable[..., Any]:
        return lambda *content: to_sandbox(builder(*content))

    functions = {
        "get_model": get_model,
        "get_embedding_model": get_embedding_model,
        "get_decision_model": get_decision_model,
        "system": message(system),
        "user": message(user),
        "assistant": message(assistant),
    }
    for name, function in functions.items():
        function.__name__ = f"republic.{name}"
    namespace = Record(
        **functions,
        **{cls.__name__: t for cls, t in _MEDIA_TYPES.items()},
        Message=_MESSAGE_TYPE,
        **{cls.__name__: t for cls, t in _MODEL_TYPES.items()},
        decisions=_DECISIONS,
    )
    module = ClassInstance(namespace, eager_attrs="all")
    return module, {function.__name__: function for function in functions.values()}


def _is_republic(module: str | None) -> bool:
    return module == "republic" or (module or "").startswith("republic.")


def _import_assignments(node: ast.Import | ast.ImportFrom) -> list[str] | None:
    """Statements replacing ``node``, or None when it imports nothing from ``republic``."""
    if isinstance(node, ast.ImportFrom):
        if (
            node.level
            or not _is_republic(node.module)
            or any(alias.name == "*" for alias in node.names)
        ):
            return None
        return [
            f"{alias.asname or alias.name} = {node.module}.{alias.name}"
            for alias in node.names
        ]
    if not any(_is_republic(alias.name) for alias in node.names):
        return None
    statements = []
    for alias in node.names:
        if not _is_republic(alias.name):
            statements.append(
                f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else "")
            )
        elif alias.asname:
            statements.append(f"{alias.asname} = {alias.name}")
    # `import republic` (or `republic.decisions`) binds the preset global itself.
    return statements or ["pass"]


def rewrite_republic_imports(code: str) -> str:
    """Replace imports from ``republic`` with assignments from the preset ``republic`` global.

    Line numbers are kept, so tracebacks point at the code as written. Code that CPython
    cannot parse is returned unchanged, for Monty to report.
    """
    try:
        tree = compile(
            code,
            "<run_code>",
            "exec",
            ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
        )
    except (SyntaxError, ValueError):
        return code
    edits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            statements = _import_assignments(node)
            if statements is not None:
                edits.append((node, "; ".join(statements)))
    if not edits:
        return code
    # AST offsets are in UTF-8 bytes.
    lines = [line.encode() for line in code.splitlines(keepends=True)]
    for node, replacement in sorted(
        edits, key=lambda e: (e[0].lineno, e[0].col_offset), reverse=True
    ):
        first, last = node.lineno - 1, node.end_lineno - 1  # type: ignore[operator]
        head = lines[first][: node.col_offset]
        tail = lines[last][node.end_col_offset :]
        # Continuation lines keep the statement on its original lines.
        filler = b" \\\n" * (last - first)
        lines[first : last + 1] = [head + replacement.encode() + filler + tail]
    return b"".join(lines).decode()
