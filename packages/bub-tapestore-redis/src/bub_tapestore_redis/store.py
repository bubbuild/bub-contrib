"""Redis-backed async TapeStore implementation for Bub."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Sequence
from contextlib import suppress
from datetime import UTC, datetime, time
from datetime import date as date_type
from typing import Any

import redis.asyncio as redis
from republic.core.errors import ErrorKind
from republic.core.results import ErrorPayload
from republic.tape.entries import TapeEntry
from republic.tape.query import TapeQuery

DEFAULT_KEY_PREFIX = "republic:tape"
_ANCHOR_SEPARATOR = ":"
_ANCHOR_ID_WIDTH = 20

# - {prefix}:tapes tracks all known tape names as an eventually consistent set
# - tape entries are stored as append-only JSON payloads
# - per-tape next_id allocates monotonically increasing entry ids
# - per-tape anchors live in one zset with score=id and
#   member="{encoded-name}:{zero-padded-id}" so repeated names remain distinct


class RedisTapeStore:
    """Async TapeStore implementation backed by Redis."""

    def __init__(
        self, client: redis.Redis, *, key_prefix: str = DEFAULT_KEY_PREFIX
    ) -> None:
        self._client = client
        self._keys = _RedisKeyspace(key_prefix)

    async def list_tapes(self) -> list[str]:
        values = await self._client.smembers(self._keys.tapes)
        return sorted(_decode_text(value) for value in values)

    async def reset(self, tape: str) -> None:
        await self._client.delete(
            self._keys.entries(tape),
            self._keys.next_id(tape),
            self._keys.anchors(tape),
        )
        # Reset clears the tape atomically in Redis; registry cleanup is only eventual.
        with suppress(Exception):
            await self._client.srem(self._keys.tapes, tape)

    async def fetch_all(self, query: TapeQuery[Any]) -> Iterable[TapeEntry]:
        start_index, end_index = await _resolve_slice_bounds(
            self._client, self._keys, query
        )
        if end_index >= 0 and end_index < start_index:
            return []

        raw_entries = await self._client.lrange(
            self._keys.entries(query.tape), start_index, end_index
        )
        entries = [_deserialize_entry(value) for value in raw_entries]
        return _apply_query(entries, query)

    async def append(self, tape: str, entry: TapeEntry) -> None:
        # Lua keeps id allocation and anchor zset maintenance atomic so concurrent
        # writers cannot assign duplicate ids or leave the anchor index behind.
        await self._client.eval(
            self._append_entry_script(),
            3,
            self._keys.next_id(tape),
            self._keys.entries(tape),
            self._keys.anchors(tape),
            _serialize_entry(entry),
            # The script insert the anchor index member if the entry is an anchor.
            _anchor_index_member_prefix(entry),
        )
        # Tape registry is best-effort. The tape data itself is already durable if
        # this update fails, so append should not raise after commit.
        with suppress(Exception):
            await self._client.sadd(self._keys.tapes, tape)

    @staticmethod
    def _append_entry_script() -> str:
        return f"""
local entry = cjson.decode(ARGV[1])
local next_id = redis.call('INCR', KEYS[1])
entry['id'] = next_id
local encoded = cjson.encode(entry)
redis.call('RPUSH', KEYS[2], encoded)
if ARGV[2] ~= '' then
  redis.call('ZADD', KEYS[3], next_id, ARGV[2] .. string.format('%0{_ANCHOR_ID_WIDTH}d', next_id))
end
return encoded
"""


class _RedisKeyspace:
    """
    Keys with the same prefix land in the same Redis slot, which avoids
    CROSSSLOT errors for Redis multi-key operations.
    Each tape owns one namespace:
    - {{slot_tag}}:{prefix}:{encoded-tape-name}
    Keys under that namespace are:
    - {namespace}:entries
    - {namespace}:next_id
    - {namespace}:anchors
    Example full keys:
    - {{slot_tag}}:{prefix}:{encoded-tape-name}:entries
    - {{slot_tag}}:{prefix}:{encoded-tape-name}:next_id
    - {{slot_tag}}:{prefix}:{encoded-tape-name}:anchors
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = _normalize_prefix(prefix)
        self._slot_tag = _encode_key_part(self._prefix)

    @property
    def tapes(self) -> str:
        return f"{{{self._slot_tag}}}:{self._prefix}:tapes"

    def entries(self, tape: str) -> str:
        return self._tape_key(tape, "entries")

    def next_id(self, tape: str) -> str:
        return self._tape_key(tape, "next_id")

    def anchors(self, tape: str) -> str:
        return self._tape_key(tape, "anchors")

    def _tape_key(self, tape: str, suffix: str) -> str:
        return f"{{{self._slot_tag}}}:{self._tape_namespace(tape)}:{suffix}"

    def _tape_namespace(self, tape: str) -> str:
        return f"{self._prefix}:{self._encoded_tape_name(tape)}"

    def _encoded_tape_name(self, tape: str) -> str:
        return _encode_key_part(tape)


def _normalize_prefix(value: str) -> str:
    stripped = value.strip(":")
    return stripped or DEFAULT_KEY_PREFIX


def _encode_key_part(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _decode_text(value: Any) -> str:
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8")
    return str(value)


def _serialize_entry(entry: TapeEntry) -> str:
    return json.dumps(
        {
            "id": entry.id,
            "kind": entry.kind,
            "payload": entry.payload,
            "meta": entry.meta,
            "date": entry.date,
        },
        sort_keys=True,
    )


def _deserialize_entry(value: Any) -> TapeEntry:
    raw = json.loads(_decode_text(value))
    return TapeEntry(
        id=int(raw["id"]),
        kind=str(raw["kind"]),
        payload=dict(raw.get("payload", {})),
        meta=dict(raw.get("meta", {})),
        date=str(raw["date"]),
    )


def _encode_anchor_index_name(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii")


def _anchor_index_member_pattern(name: str) -> str:
    return f"{_encode_anchor_index_name(name)}{_ANCHOR_SEPARATOR}*"


def _parse_anchor_index_member_id(value: Any) -> int:
    member = _decode_text(value)
    _, entry_id = member.rsplit(_ANCHOR_SEPARATOR, 1)
    return int(entry_id)


def _anchor_index_member_prefix(entry: TapeEntry) -> str:
    if entry.kind != "anchor":
        return ""

    name = entry.payload.get("name")
    if name is None:
        return ""
    return f"{_encode_anchor_index_name(str(name))}{_ANCHOR_SEPARATOR}"


async def _resolve_slice_bounds(
    client: redis.Redis,
    keys: _RedisKeyspace,
    query: TapeQuery[Any],
) -> tuple[int, int]:
    anchor_key = keys.anchors(query.tape)

    if query._between_anchors is not None:
        return await _resolve_between_anchor_bounds(
            client, anchor_key, *query._between_anchors
        )

    if query._after_last:
        anchor_id = await _last_anchor_id(client, anchor_key)
        if anchor_id < 0:
            raise ErrorPayload(ErrorKind.NOT_FOUND, "No anchors found in tape.")
        return anchor_id, -1

    if query._after_anchor is not None:
        anchor_id = max(
            await _scan_anchor_ids(
                client, anchor_key, _anchor_index_member_pattern(query._after_anchor)
            ),
            default=-1,
        )
        if anchor_id < 0:
            raise ErrorPayload(
                ErrorKind.NOT_FOUND, f"Anchor '{query._after_anchor}' was not found."
            )
        return anchor_id, -1

    return 0, -1


async def _resolve_between_anchor_bounds(
    client: redis.Redis,
    anchor_key: str,
    start_name: str,
    end_name: str,
) -> tuple[int, int]:
    start_ids = await _scan_anchor_ids(
        client, anchor_key, _anchor_index_member_pattern(start_name)
    )
    start_id = max(start_ids, default=-1)
    if start_id < 0:
        raise ErrorPayload(ErrorKind.NOT_FOUND, f"Anchor '{start_name}' was not found.")

    end_ids = await _scan_anchor_ids(
        client, anchor_key, _anchor_index_member_pattern(end_name)
    )
    end_candidates = [entry_id for entry_id in end_ids if entry_id > start_id]
    end_id = min(end_candidates, default=-1)
    if end_id < 0:
        raise ErrorPayload(ErrorKind.NOT_FOUND, f"Anchor '{end_name}' was not found.")
    return start_id, end_id - 2


async def _scan_anchor_ids(client: redis.Redis, key: str, pattern: str) -> list[int]:
    cursor = 0
    ids: list[int] = []
    while True:
        cursor, values = await client.zscan(key, cursor=cursor, match=pattern)
        ids.extend(_parse_anchor_index_member_id(member) for member, _score in values)
        if cursor == 0:
            return ids


async def _last_anchor_id(client: redis.Redis, key: str) -> int:
    values = await client.zrevrange(key, 0, 0)
    return _parse_anchor_index_member_id(values[0]) if values else -1


def _apply_query(
    entries: Sequence[TapeEntry], query: TapeQuery[Any]
) -> list[TapeEntry]:
    # Anchor boundaries are resolved in Redis before loading entries. The remaining
    # Republic query contract still runs in Python for date/text/kind/limit filters.
    sliced = list(entries)

    if query._between_dates is not None:
        start_date, end_date = query._between_dates
        start_dt = _parse_datetime_boundary(start_date, is_end=False)
        end_dt = _parse_datetime_boundary(end_date, is_end=True)
        if start_dt > end_dt:
            raise ErrorPayload(
                ErrorKind.INVALID_INPUT,
                "Start date must be earlier than or equal to end date.",
            )
        sliced = [
            entry
            for entry in sliced
            if _entry_in_datetime_range(entry, start_dt, end_dt)
        ]

    if query._query:
        sliced = [
            entry for entry in sliced if _entry_matches_query(entry, query._query)
        ]

    if query._kinds:
        sliced = [entry for entry in sliced if entry.kind in query._kinds]

    if query._limit is not None:
        sliced = sliced[: query._limit]

    return sliced


def _parse_datetime_boundary(value: str, *, is_end: bool) -> datetime:
    if "T" not in value and " " not in value:
        try:
            parsed_date = date_type.fromisoformat(value)
        except ValueError:
            pass
        else:
            boundary_time = time.max if is_end else time.min
            return datetime.combine(parsed_date, boundary_time, tzinfo=UTC)

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed_date = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ErrorPayload(
                ErrorKind.INVALID_INPUT, f"Invalid ISO date or datetime: '{value}'."
            ) from exc
        boundary_time = time.max if is_end else time.min
        parsed = datetime.combine(parsed_date, boundary_time, tzinfo=UTC)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entry_in_datetime_range(
    entry: TapeEntry, start_dt: datetime, end_dt: datetime
) -> bool:
    entry_dt = _parse_datetime_boundary(entry.date, is_end=False)
    return start_dt <= entry_dt <= end_dt


def _entry_matches_query(entry: TapeEntry, query: str) -> bool:
    needle = query.casefold()
    haystack = json.dumps(
        {
            "kind": entry.kind,
            "date": entry.date,
            "payload": entry.payload,
            "meta": entry.meta,
        },
        sort_keys=True,
        default=str,
    ).casefold()
    return needle in haystack
