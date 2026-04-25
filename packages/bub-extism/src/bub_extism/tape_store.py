from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from republic import TapeEntry

from bub_extism.bridge import ExtismBridge
from bub_extism.codec import tape_entry_from_dict, tape_entry_to_dict, to_json_value
from bub_extism.config import ExtismPluginConfig

if TYPE_CHECKING:
    from republic import TapeQuery


class ExtismTapeStore:
    def __init__(
        self,
        bridge: ExtismBridge,
        config: ExtismPluginConfig,
        descriptor: dict[str, Any],
    ) -> None:
        self.bridge = bridge
        self.config = config
        self.descriptor = descriptor
        self.functions = dict(descriptor.get("functions") or {})

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
        return [tape_entry_from_dict(item) for item in value if isinstance(item, dict)]

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


def tape_store_from_descriptor(
    bridge: ExtismBridge,
    config: ExtismPluginConfig,
    descriptor: Any,
) -> ExtismTapeStore | None:
    if descriptor is None:
        return None
    if not isinstance(descriptor, dict):
        raise RuntimeError("Extism provide_tape_store must return a descriptor object")
    return ExtismTapeStore(bridge, config, descriptor)


def _query_to_dict(query: TapeQuery) -> dict[str, Any]:
    return {
        "tape": query.tape,
        "query": query._query,
        "after_anchor": query._after_anchor,
        "after_last": query._after_last,
        "between_anchors": to_json_value(query._between_anchors),
        "between_dates": to_json_value(query._between_dates),
        "kinds": list(query._kinds),
        "limit": query._limit,
    }
