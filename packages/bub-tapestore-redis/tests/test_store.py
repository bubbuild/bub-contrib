from __future__ import annotations

import uuid
from datetime import date

import pytest
import pytest_asyncio
from bub_tapestore_redis import RedisTapeStore
from fakeredis import FakeAsyncRedis
from redis.crc import key_slot
from republic import AsyncTapeManager, RepublicError, TapeContext, TapeEntry, TapeQuery
from republic.core.errors import ErrorKind
from republic.tape.context import LAST_ANCHOR


def _unique_prefix() -> str:
    return f"test:bub-tapestore-redis:{uuid.uuid4().hex}"


def _seed_entries() -> list[TapeEntry]:
    return [
        TapeEntry.message({"role": "user", "content": "before"}),
        TapeEntry.anchor("a1"),
        TapeEntry.message({"role": "user", "content": "task 1"}),
        TapeEntry.message({"role": "assistant", "content": "answer 1"}),
        TapeEntry.anchor("a2"),
        TapeEntry.message({"role": "user", "content": "task 2"}),
    ]


async def _connect() -> FakeAsyncRedis:
    return FakeAsyncRedis()


async def _cleanup(client: FakeAsyncRedis, prefix: str) -> None:
    keys = [key async for key in client.scan_iter(f"{prefix}:*")]
    keys.extend([key async for key in client.scan_iter(f"*:{prefix}:*")])
    if keys:
        await client.delete(*keys)


@pytest_asyncio.fixture
async def store() -> RedisTapeStore:
    client = await _connect()
    prefix = _unique_prefix()
    tape_store = RedisTapeStore(client, key_prefix=prefix)
    try:
        yield tape_store
    finally:
        await _cleanup(client, prefix)
        await client.aclose()


@pytest.mark.asyncio
async def test_list_tapes_returns_sorted_names(store: RedisTapeStore) -> None:
    await store.append("beta", TapeEntry.message({"role": "user", "content": "hello"}))
    await store.append(
        "alpha", TapeEntry.message({"role": "assistant", "content": "world"})
    )

    assert await store.list_tapes() == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_tape_local_keys_share_one_cluster_hash_slot(
    store: RedisTapeStore,
) -> None:
    keys = [
        store._keys.entries("session"),
        store._keys.next_id("session"),
        store._keys.anchors("session"),
    ]

    assert len({key_slot(key.encode("utf-8")) for key in keys}) == 1


@pytest.mark.asyncio
async def test_tape_names_with_braces_do_not_collide(store: RedisTapeStore) -> None:
    brace_tape = "a{b"
    plain_tape = "a_b"

    await store.append(
        brace_tape, TapeEntry.message({"role": "user", "content": "brace"})
    )
    await store.append(
        plain_tape, TapeEntry.message({"role": "user", "content": "plain"})
    )

    brace_entries = list(await TapeQuery(tape=brace_tape, store=store).all())
    plain_entries = list(await TapeQuery(tape=plain_tape, store=store).all())

    assert [entry.payload["content"] for entry in brace_entries] == ["brace"]
    assert [entry.payload["content"] for entry in plain_entries] == ["plain"]
    assert store._keys.entries(brace_tape) != store._keys.entries(plain_tape)


@pytest.mark.asyncio
async def test_append_assigns_incrementing_ids_and_reset_restarts(
    store: RedisTapeStore,
) -> None:
    await store.append(
        "session", TapeEntry.message({"role": "user", "content": "first"})
    )
    await store.append(
        "session", TapeEntry.message({"role": "assistant", "content": "second"})
    )

    first_pass = list(await TapeQuery(tape="session", store=store).all())
    assert [entry.id for entry in first_pass] == [1, 2]

    await store.reset("session")
    await store.append(
        "session", TapeEntry.message({"role": "user", "content": "third"})
    )

    second_pass = list(await TapeQuery(tape="session", store=store).all())
    assert [entry.id for entry in second_pass] == [1]
    assert second_pass[0].payload["content"] == "third"


@pytest.mark.asyncio
async def test_fetch_all_matches_republic_query_contract(store: RedisTapeStore) -> None:
    tape = "contract"
    for entry in _seed_entries():
        await store.append(tape, entry)

    entries = list(
        await TapeQuery(tape=tape, store=store)
        .between_anchors("a1", "a2")
        .kinds("message")
        .limit(1)
        .all()
    )
    assert len(entries) == 1
    assert entries[0].payload["content"] == "task 1"


@pytest.mark.asyncio
async def test_query_text_matches_payload_and_meta(store: RedisTapeStore) -> None:
    tape = "searchable"

    await store.append(
        tape,
        TapeEntry.message(
            {"role": "user", "content": "Database timeout on checkout"}, scope="db"
        ),
    )
    await store.append(tape, TapeEntry.event("run", {"status": "ok"}, scope="system"))

    entries = list(await TapeQuery(tape=tape, store=store).query("timeout").all())
    assert [entry.kind for entry in entries] == ["message"]

    meta_entries = list(await TapeQuery(tape=tape, store=store).query("system").all())
    assert [entry.kind for entry in meta_entries] == ["event"]


@pytest.mark.asyncio
async def test_query_between_dates_is_inclusive(store: RedisTapeStore) -> None:
    tape = "dated"

    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="message",
            payload={"role": "user", "content": "before"},
            date="2026-03-01T08:00:00+00:00",
        ),
    )
    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="message",
            payload={"role": "user", "content": "during"},
            date="2026-03-02T09:30:00+00:00",
        ),
    )
    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="message",
            payload={"role": "user", "content": "after"},
            date="2026-03-04T18:45:00+00:00",
        ),
    )

    entries = list(
        await TapeQuery(tape=tape, store=store)
        .between_dates(date(2026, 3, 2), "2026-03-03")
        .all()
    )
    assert [entry.payload["content"] for entry in entries] == ["during"]


@pytest.mark.asyncio
async def test_query_combines_anchor_date_and_text_filters(
    store: RedisTapeStore,
) -> None:
    tape = "combined"

    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="anchor",
            payload={"name": "a1"},
            date="2026-03-01T00:00:00+00:00",
        ),
    )
    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="message",
            payload={"role": "user", "content": "old timeout"},
            date="2026-03-01T12:00:00+00:00",
        ),
    )
    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="anchor",
            payload={"name": "a2"},
            date="2026-03-02T00:00:00+00:00",
        ),
    )
    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="message",
            payload={"role": "user", "content": "new timeout"},
            meta={"source": "ops"},
            date="2026-03-02T12:00:00+00:00",
        ),
    )
    await store.append(
        tape,
        TapeEntry(
            id=0,
            kind="message",
            payload={"role": "user", "content": "new success"},
            meta={"source": "ops"},
            date="2026-03-03T12:00:00+00:00",
        ),
    )

    entries = list(
        await TapeQuery(tape=tape, store=store)
        .after_anchor("a2")
        .between_dates("2026-03-02", "2026-03-02")
        .query("timeout")
        .all()
    )
    assert [entry.payload["content"] for entry in entries] == ["new timeout"]


@pytest.mark.asyncio
async def test_missing_anchor_and_invalid_date_raise_republic_error(
    store: RedisTapeStore,
) -> None:
    with pytest.raises(RepublicError) as missing_anchor:
        list(await TapeQuery(tape="empty", store=store).after_anchor("missing").all())
    assert missing_anchor.value.kind == ErrorKind.NOT_FOUND

    await store.append(
        "dated",
        TapeEntry(
            id=0,
            kind="message",
            payload={"role": "user", "content": "entry"},
            date="2026-03-03T00:00:00+00:00",
        ),
    )
    with pytest.raises(RepublicError) as invalid_range:
        list(
            await TapeQuery(tape="dated", store=store)
            .between_dates("2026-03-05", "2026-03-01")
            .all()
        )
    assert invalid_range.value.kind == ErrorKind.INVALID_INPUT


@pytest.mark.asyncio
async def test_duplicate_anchor_names_use_latest_start_and_first_following_end(
    store: RedisTapeStore,
) -> None:
    tape = "duplicate-anchors"
    entries = [
        TapeEntry.anchor("start"),
        TapeEntry.message({"role": "user", "content": "old block"}),
        TapeEntry.anchor("start"),
        TapeEntry.message({"role": "user", "content": "kept"}),
        TapeEntry.anchor("end"),
        TapeEntry.message({"role": "user", "content": "after"}),
        TapeEntry.anchor("end"),
    ]

    for entry in entries:
        await store.append(tape, entry)

    after_start = list(
        await TapeQuery(tape=tape, store=store).after_anchor("start").all()
    )
    assert [
        entry.payload["content"] for entry in after_start if entry.kind == "message"
    ] == ["kept", "after"]

    between = list(
        await TapeQuery(tape=tape, store=store).between_anchors("start", "end").all()
    )
    assert [
        entry.payload["content"] for entry in between if entry.kind == "message"
    ] == ["kept"]


@pytest.mark.asyncio
async def test_reset_clears_anchor_indexes(store: RedisTapeStore) -> None:
    await store.append("session", TapeEntry.anchor("a1"))
    await store.reset("session")
    await store.append(
        "session", TapeEntry.message({"role": "user", "content": "fresh"})
    )

    with pytest.raises(RepublicError) as exc_info:
        list(await TapeQuery(tape="session", store=store).after_anchor("a1").all())
    assert exc_info.value.kind == ErrorKind.NOT_FOUND


@pytest.mark.asyncio
async def test_empty_string_anchor_is_indexed(store: RedisTapeStore) -> None:
    tape = "empty-anchor"
    await store.append(tape, TapeEntry.anchor(""))
    await store.append(
        tape, TapeEntry.message({"role": "user", "content": "after empty"})
    )

    entries = list(await TapeQuery(tape=tape, store=store).after_anchor("").all())
    assert [
        entry.payload["content"] for entry in entries if entry.kind == "message"
    ] == ["after empty"]


@pytest.mark.asyncio
async def test_tapemanager_reads_last_anchor_slice(store: RedisTapeStore) -> None:
    for entry in _seed_entries():
        await store.append("test_tape", entry)

    manager = AsyncTapeManager(store=store)
    messages = await manager.read_messages(
        "test_tape", context=TapeContext(anchor=LAST_ANCHOR)
    )
    assert [message["content"] for message in messages] == ["task 2"]
