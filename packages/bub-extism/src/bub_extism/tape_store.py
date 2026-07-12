from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from bub.tape import TapeEntry

from bub_extism.bridge import ExtismBridge
from bub_extism.codec import tape_entry_from_dict, tape_entry_to_dict
from bub_extism.config import ExtismPluginConfig
from bub_extism.descriptors import normalize_function_bindings, require_mapping

if TYPE_CHECKING:
    from bub.tape import TapeQuery


class ExtismTapeStore:
    def __init__(
        self,
        bridge: ExtismBridge,
        config: ExtismPluginConfig,
        *,
        functions: dict[str, str],
    ) -> None:
        self.bridge = bridge
        self.config = config
        self.functions = functions

    @classmethod
    def from_descriptor(
        cls,
        bridge: ExtismBridge,
        config: ExtismPluginConfig,
        descriptor: Any,
    ) -> ExtismTapeStore:
        data = require_mapping(
            descriptor,
            message="Extism provide_tape_store must return a descriptor object",
        )
        return cls(bridge, config, functions=_functions_from_descriptor(data))

    def list_tapes(self) -> list[str]:
        value = self._call("list_tapes", {})
        if value is None:
            return []
        if not isinstance(value, list):
            raise RuntimeError("Extism tape list_tapes must return a list")
        return [str(item) for item in value]

    def reset(self, tape: str) -> None:
        self._call("reset", {"tape": tape})

    def fetch_all(self, query: TapeQuery) -> Iterable[TapeEntry]:
        value = self._call("fetch_all", {"query": _query_to_dict(query)})
        if value is None:
            return []
        if not isinstance(value, list):
            raise RuntimeError("Extism tape fetch_all must return a list")
        return [
            tape_entry_from_dict(
                require_mapping(item, message="Extism tape entry must be an object")
            )
            for item in value
        ]

    def append(self, tape: str, entry: TapeEntry) -> None:
        self._call("append", {"tape": tape, "entry": tape_entry_to_dict(entry)})

    def _call(self, operation: str, args: dict[str, Any]) -> Any:
        function_name = self.functions.get(operation)
        if not isinstance(function_name, str) or not function_name:
            raise RuntimeError(f"Extism tape store does not define '{operation}'")
        return self.bridge.call_hook_sync(
            f"tape_store.{operation}",
            args,
            config=self.config,
            function_name=function_name,
        )


def tape_store_from_value(
    bridge: ExtismBridge,
    config: ExtismPluginConfig,
    value: Any,
) -> ExtismTapeStore | None:
    if value is None:
        return None
    return ExtismTapeStore.from_descriptor(bridge, config, value)


def _functions_from_descriptor(descriptor: dict[str, Any]) -> dict[str, str]:
    return normalize_function_bindings(
        descriptor.get("functions"),
        message="Extism tape store descriptor must include a functions object",
        missing_ok=False,
    )


def _query_to_dict(query: TapeQuery) -> dict[str, Any]:
    return {
        "tape": query.tape,
        "query": query._query,
        "after_anchor": query._after_anchor,
        "after_last": query._after_last,
        "between_anchors": list(query._between_anchors)
        if query._between_anchors is not None
        else None,
        "between_dates": list(query._between_dates)
        if query._between_dates is not None
        else None,
        "kinds": list(query._kinds),
        "limit": query._limit,
    }
