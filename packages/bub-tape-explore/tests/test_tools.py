from __future__ import annotations

import json
from pathlib import Path

from republic.tape.entries import TapeEntry

from bub_tape_explore.explore import build_explore
from bub_tape_explore.map import build_nodes, find_node
from bub_tape_explore.presenters import render_explore, render_map, render_window
from bub_tape_explore.window import select_window_entries


def _sample_entries() -> list[TapeEntry]:
    root = Path(__file__).resolve().parents[4]
    sample_path = root / "tape_sample.jsonl"
    entries: list[TapeEntry] = []
    for raw_line in sample_path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(raw_line)
        entries.append(
            TapeEntry(
                id=int(payload["id"]),
                kind=str(payload["kind"]),
                payload=dict(payload.get("payload", {})),
                meta=dict(payload.get("meta", {})),
                date=str(payload.get("date", "")),
            )
        )
    return entries


def test_map_compresses_sample_into_run_nodes() -> None:
    nodes = build_nodes(_sample_entries())

    assert len(nodes) == 5
    assert nodes[0].start_entry_id == 8639
    assert nodes[0].end_entry_id == 8644
    assert nodes[-1].start_entry_id == 8615
    assert nodes[-1].end_entry_id == 8618
    assert {node.status for node in nodes} == {"continue", "ok"}


def test_render_map_lists_recent_nodes_first() -> None:
    nodes = build_nodes(_sample_entries(), limit=2)
    rendered = render_map("sample", nodes)

    assert "node_id: run:6ecc9611ea41499cb883a9197b0a9b5c" in rendered
    assert "node_id: run:d29f378c20e4499498cb271580f16217" in rendered
    assert 'label: assistant: 文件已发送。' in rendered


def test_explore_returns_before_after_and_stats() -> None:
    entries = _sample_entries()
    nodes = build_nodes(entries)
    items = build_explore(
        entries,
        nodes,
        limit=1,
        node_ids=["run:d29f378c20e4499498cb271580f16217"],
        before_messages=1,
        after_items=3,
    )
    rendered = render_explore("sample", items)

    assert "before:" in rendered
    assert "after:" in rendered
    assert "stats:" in rendered
    assert "user: 你用文件上传形式吧" in rendered
    assert "loop.step: continue" in rendered
    assert "status: continue" in rendered
    assert "why" not in rendered
    assert "action" not in rendered


def test_window_returns_raw_entries_for_node() -> None:
    entries = _sample_entries()
    nodes = build_nodes(entries)
    node = find_node(nodes, "run:d29f378c20e4499498cb271580f16217")

    assert node is not None
    rendered = render_window(node, select_window_entries(entries, node))

    assert "[8635][tool_call] tool_call: bash" in rendered
    assert "[8638][event] loop.step status=continue" in rendered


def test_window_supports_limit_and_filter() -> None:
    entries = _sample_entries()
    nodes = build_nodes(entries)
    node = find_node(nodes, "run:d29f378c20e4499498cb271580f16217")

    assert node is not None
    rendered = render_window(
        node,
        select_window_entries(entries, node, limit=2, filter_text="tool"),
        filter_text="tool",
    )

    assert "filter: tool" in rendered
    assert "[8635][tool_call] tool_call: bash" in rendered
    assert "[8636][tool_result]" in rendered
    assert "[8638][event] loop.step status=continue" not in rendered
