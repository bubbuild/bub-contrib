from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from republic import RepublicError, TapeContext, TapeEntry, TapeQuery

from bub_tapestore_sqlite.store import SQLiteTapeStore


async def _store(tmp_path: Path) -> SQLiteTapeStore:
    return SQLiteTapeStore(tmp_path / "tapes.sqlite3")


async def _semantic_store(tmp_path: Path) -> SQLiteTapeStore:
    return SQLiteTapeStore(
        tmp_path / "tapes.sqlite3",
        embedding_model="openai:text-embedding-3-small",
    )


def _embedding_response(vectors: list[list[float]]) -> SimpleNamespace:
    return SimpleNamespace(
        data=[SimpleNamespace(embedding=vector) for vector in vectors],
    )


def _mock_embedding_client(monkeypatch, fake_aembedding):
    from bub.builtin import agent as agent_module

    build_calls: list[TapeContext] = []

    def fake_build_llm(settings, tape_store, tape_context):
        del settings, tape_store
        assert isinstance(tape_context, TapeContext)
        build_calls.append(tape_context)
        return SimpleNamespace(
            _core=SimpleNamespace(
                get_client=lambda provider: SimpleNamespace(
                    aembedding=fake_aembedding,
                )
            )
        )

    monkeypatch.setattr(agent_module, "_build_llm", fake_build_llm)
    return build_calls


def test_append_list_and_reset_tapes(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        await store.append("a__1", TapeEntry.message({"content": "hello"}))
        await store.append("b__2", TapeEntry.system("world"))

        assert await store.list_tapes() == ["a__1", "b__2"]

        await store.reset("a__1")

        assert await store.list_tapes() == ["b__2"]
        assert list(await TapeQuery("a__1", store).all()) == []
        await store.close()

    asyncio.run(scenario())


def test_assigns_global_autoincrement_ids(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        await store.append("room__1", TapeEntry.message({"content": "first"}))
        await store.append("room__1", TapeEntry.system("second"))
        await store.append("other__1", TapeEntry.system("third"))

        entries = list(await TapeQuery("room__1", store).all())
        other_entries = list(await TapeQuery("other__1", store).all())

        assert [entry.id for entry in entries] == [1, 2]
        assert [entry.id for entry in other_entries] == [3]
        await store.close()

    asyncio.run(scenario())


def test_persists_text_lookup_field(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        await store.append(
            "room__text",
            TapeEntry.message({"content": "from content"}),
        )
        await store.append(
            "room__text",
            TapeEntry.tool_result(["first line", {"nested": "second line"}]),
        )
        await store.append(
            "room__text",
            TapeEntry.event("handoff", {"state": {"owner": "human"}}),
        )
        await store.close()

    asyncio.run(scenario())

    database = sqlite3.connect(tmp_path / "tapes.sqlite3")
    try:
        rows = database.execute(
            "SELECT text FROM tape_entries ORDER BY id"
        ).fetchall()
    finally:
        database.close()

    assert rows == [
        ("from content",),
        ("first line\nsecond line",),
        ("handoff\nhuman",),
    ]


def test_query_after_anchor_and_last_anchor(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        tape = "session__1"
        await store.append(tape, TapeEntry.system("boot"))
        await store.append(tape, TapeEntry.anchor("phase-1"))
        await store.append(tape, TapeEntry.message({"content": "alpha"}))
        await store.append(tape, TapeEntry.anchor("phase-2"))
        await store.append(tape, TapeEntry.message({"content": "beta"}))

        after_phase_1 = list(await TapeQuery(tape, store).after_anchor("phase-1").all())
        after_last = list(await TapeQuery(tape, store).last_anchor().all())

        assert [entry.kind for entry in after_phase_1] == [
            "message",
            "anchor",
            "message",
        ]
        assert [entry.payload.get("content") for entry in after_last] == ["beta"]
        await store.close()

    asyncio.run(scenario())


def test_query_between_anchors_kinds_and_limit(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        tape = "session__2"
        await store.append(tape, TapeEntry.anchor("start"))
        await store.append(tape, TapeEntry.system("skip"))
        await store.append(tape, TapeEntry.message({"content": "one"}))
        await store.append(tape, TapeEntry.message({"content": "two"}))
        await store.append(tape, TapeEntry.anchor("end"))
        await store.append(tape, TapeEntry.message({"content": "three"}))

        entries = list(
            await TapeQuery(tape, store)
            .between_anchors("start", "end")
            .kinds("message")
            .limit(1)
            .all()
        )

        assert len(entries) == 1
        assert entries[0].payload == {"content": "one"}
        await store.close()

    asyncio.run(scenario())


def test_query_missing_anchor_matches_existing_error_shape(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        tape = "session__3"
        await store.append(tape, TapeEntry.message({"content": "hello"}))

        with pytest.raises(RepublicError, match="Anchor 'missing' was not found."):
            await TapeQuery(tape, store).after_anchor("missing").all()

        await store.close()

    asyncio.run(scenario())


def test_store_constructor_validates_modes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BUB_SQLITE_JOURNAL_MODE"):
        SQLiteTapeStore(tmp_path / "invalid.sqlite3", journal_mode="invalid")


def test_semantic_query_flows_through_fetch_all(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        calls: list[list[str]] = []
        kwargs_seen: list[dict[str, object]] = []

        async def fake_aembedding(model: str, inputs, **kwargs) -> SimpleNamespace:
            calls.append(inputs)
            kwargs_seen.append(dict(kwargs))
            if inputs == ["alpha", "beta"]:
                assert inputs == ["alpha", "beta"]
                return _embedding_response(
                    [
                        [1.0, 0.0, 0.0],
                        [0.8, 0.2, 0.0],
                    ]
                )
            assert inputs == ["alpha question"]
            return _embedding_response([[0.9, 0.1, 0.0]])

        build_calls = _mock_embedding_client(monkeypatch, fake_aembedding)
        store = await _semantic_store(tmp_path)
        tape = "session__vec"
        await store.append(tape, TapeEntry.message({"content": "alpha"}))
        await store.append(tape, TapeEntry.message({"content": "beta"}))
        await store.append(tape, TapeEntry.system("ignored"))

        entries = list(
            await TapeQuery(tape, store)
            .query("alpha question")
            .kinds("message")
            .limit(2)
            .all()
        )

        assert [entry.payload.get("content") for entry in entries] == [
            "beta",
            "alpha",
        ]
        assert calls == [["alpha", "beta"], ["alpha question"]]
        assert kwargs_seen == [{}, {}]
        assert len(build_calls) == 1
        await store.close()

    asyncio.run(scenario())


def test_semantic_query_skips_reembedding_indexed_messages(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        calls: list[list[str]] = []

        async def fake_aembedding(model: str, inputs, **kwargs) -> SimpleNamespace:
            calls.append(inputs)
            if isinstance(inputs, list):
                return _embedding_response([[1.0, 0.0, 0.0]])
            return _embedding_response([[1.0, 0.0, 0.0]])

        build_calls = _mock_embedding_client(monkeypatch, fake_aembedding)
        store = await _semantic_store(tmp_path)
        tape = "session__vec_skip"
        await store.append(tape, TapeEntry.message({"content": "alpha"}))

        first = list(await TapeQuery(tape, store).query("alpha").all())
        second = list(await TapeQuery(tape, store).query("alpha").all())

        assert len(first) == 1
        assert len(second) == 1
        assert calls == [["alpha"], ["alpha"], ["alpha"]]
        assert len(build_calls) == 1
        await store.close()

    asyncio.run(scenario())


def test_query_falls_back_to_ilike_without_embedding_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = await _store(tmp_path)
        tape = "session__text_query"
        await store.append(tape, TapeEntry.message({"content": "Alpha one"}))
        await store.append(tape, TapeEntry.tool_result(["beta result"]))
        await store.append(tape, TapeEntry.system("ALPHA two"))
        await store.append(tape, TapeEntry.anchor("alpha-anchor"))

        entries = list(
            await TapeQuery(tape, store)
            .query("alpha")
            .kinds("message", "system", "anchor")
            .limit(3)
            .all()
        )

        assert [entry.kind for entry in entries] == ["message", "system", "anchor"]

        await store.close()

    asyncio.run(scenario())


def test_semantic_query_rejects_dimension_mismatch(tmp_path: Path, monkeypatch) -> None:
    async def scenario() -> None:
        responses = [
            _embedding_response([[1.0, 0.0, 0.0], [0.8, 0.2, 0.0]]),
            _embedding_response([[0.9, 0.1, 0.0]]),
            _embedding_response([[1.0, 0.0]]),
        ]

        async def fake_aembedding(model: str, inputs, **kwargs) -> SimpleNamespace:
            return responses.pop(0)

        build_calls = _mock_embedding_client(monkeypatch, fake_aembedding)
        store = await _semantic_store(tmp_path)
        tape = "session__bad_vec"
        await store.append(tape, TapeEntry.message({"content": "alpha"}))
        await store.append(tape, TapeEntry.message({"content": "beta"}))

        assert len(list(await TapeQuery(tape, store).query("alpha").all())) == 2
        await store.append(tape, TapeEntry.message({"content": "gamma"}))
        with pytest.raises(RuntimeError, match="Embedding dimensions 2 do not match"):
            await TapeQuery(tape, store).query("alpha").all()

        assert len(build_calls) == 1
        await store.close()

    asyncio.run(scenario())
