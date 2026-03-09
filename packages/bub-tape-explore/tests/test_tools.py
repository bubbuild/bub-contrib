from __future__ import annotations

from republic.tape.entries import TapeEntry

from bub_tape_explore.explore import build_explore
from bub_tape_explore.map import build_nodes, find_node
from bub_tape_explore.presenters import render_explore, render_map, render_window
from bub_tape_explore.window import select_window_entries

CONTINUE_NODE_ID = "run:d29f378c20e4499498cb271580f16217"
LATEST_NODE_ID = "run:6ecc9611ea41499cb883a9197b0a9b5c"


def _sample_entries() -> list[TapeEntry]:
    return [
        _event(8615, "loop.step.start", run_id="c6e2f39b6f374fd595a4d7c22f7cce11"),
        _message(8616, "user", "Please upload the result.", run_id="c6e2f39b6f374fd595a4d7c22f7cce11"),
        _message(8617, "assistant", "I will prepare the file.", run_id="c6e2f39b6f374fd595a4d7c22f7cce11"),
        _event(
            8618,
            "loop.step",
            run_id="c6e2f39b6f374fd595a4d7c22f7cce11",
            status="ok",
        ),
        _event(8619, "loop.step.start", run_id="5d6e2d5030244d6b96085a4e1ef22a10"),
        _message(8620, "assistant", "I need one more command.", run_id="5d6e2d5030244d6b96085a4e1ef22a10"),
        _tool_call(8621, "bash", run_id="5d6e2d5030244d6b96085a4e1ef22a10"),
        _tool_result(8622, "command output", run_id="5d6e2d5030244d6b96085a4e1ef22a10"),
        _event(
            8623,
            "loop.step",
            run_id="5d6e2d5030244d6b96085a4e1ef22a10",
            status="continue",
        ),
        _event(8624, "loop.step.start", run_id="a81f0f21dc0d44c4803e741e95b7e001"),
        _message(8625, "assistant", "The command finished.", run_id="a81f0f21dc0d44c4803e741e95b7e001"),
        _message(8626, "user", "Please send the file directly.", run_id="a81f0f21dc0d44c4803e741e95b7e001"),
        _message(8627, "assistant", "I can do that.", run_id="a81f0f21dc0d44c4803e741e95b7e001"),
        _message(8628, "assistant", "Use file upload mode.", run_id="a81f0f21dc0d44c4803e741e95b7e001"),
        _message(8629, "user", "你用文件上传形式吧", run_id="a81f0f21dc0d44c4803e741e95b7e001"),
        _event(
            8630,
            "loop.step",
            run_id="a81f0f21dc0d44c4803e741e95b7e001",
            status="ok",
        ),
        _event(8631, "loop.step.start", run_id="d29f378c20e4499498cb271580f16217"),
        _message(8632, "assistant", "Continue the task. Use the previous result.", run_id="d29f378c20e4499498cb271580f16217"),
        _message(8633, "assistant", "Continue the task. Keep the current plan.", run_id="d29f378c20e4499498cb271580f16217"),
        _message(8634, "assistant", "Continue the task. Run the upload command.", run_id="d29f378c20e4499498cb271580f16217"),
        _tool_call(8635, "bash", run_id="d29f378c20e4499498cb271580f16217"),
        _tool_result(8636, "saved file", run_id="d29f378c20e4499498cb271580f16217"),
        _message(8637, "assistant", "The file is ready.", run_id="d29f378c20e4499498cb271580f16217"),
        _event(
            8638,
            "loop.step",
            run_id="d29f378c20e4499498cb271580f16217",
            status="continue",
        ),
        _event(8639, "loop.step.start", run_id="6ecc9611ea41499cb883a9197b0a9b5c"),
        _message(8640, "assistant", "Continue the task. Use the previous result.", run_id="6ecc9611ea41499cb883a9197b0a9b5c"),
        _message(8641, "assistant", "文件已发送。", run_id="6ecc9611ea41499cb883a9197b0a9b5c"),
        _message(8642, "assistant", "Anything else?", run_id="6ecc9611ea41499cb883a9197b0a9b5c"),
        _tool_result(8643, "upload complete", run_id="6ecc9611ea41499cb883a9197b0a9b5c"),
        _event(
            8644,
            "loop.step",
            run_id="6ecc9611ea41499cb883a9197b0a9b5c",
            status="ok",
        ),
    ]


def test_map_compresses_entries_into_run_nodes() -> None:
    nodes = build_nodes(_sample_entries())

    assert len(nodes) == 5
    assert nodes[0].node_id == LATEST_NODE_ID
    assert nodes[0].start_entry_id == 8639
    assert nodes[0].end_entry_id == 8644
    assert nodes[-1].start_entry_id == 8615
    assert nodes[-1].end_entry_id == 8618
    assert {node.status for node in nodes} == {"continue", "ok"}


def test_render_map_lists_recent_nodes_first() -> None:
    nodes = build_nodes(_sample_entries(), limit=2)
    rendered = render_map("sample", nodes)

    assert f"node_id: {LATEST_NODE_ID}" in rendered
    assert f"node_id: {CONTINUE_NODE_ID}" in rendered
    assert "label: assistant: 文件已发送。" in rendered
    assert "label: tool: bash" in rendered


def test_explore_returns_before_after_and_stats() -> None:
    entries = _sample_entries()
    nodes = build_nodes(entries)
    items = build_explore(
        entries,
        nodes,
        limit=1,
        node_ids=[CONTINUE_NODE_ID],
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
    node = find_node(nodes, CONTINUE_NODE_ID)

    assert node is not None
    rendered = render_window(node, select_window_entries(entries, node))

    assert "[8635][tool_call] tool_call: bash" in rendered
    assert "[8638][event] loop.step status=continue" in rendered


def test_window_supports_limit_and_filter() -> None:
    entries = _sample_entries()
    nodes = build_nodes(entries)
    node = find_node(nodes, CONTINUE_NODE_ID)

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


def _event(entry_id: int, name: str, *, run_id: str, status: str | None = None) -> TapeEntry:
    meta: dict[str, object] = {"run_id": run_id}
    if name == "loop.step" and status is not None:
        meta["payload"] = {"status": status}
    return TapeEntry(
        id=entry_id,
        kind="event",
        payload={"name": name},
        meta=meta,
        date="2026-03-10T00:00:00Z",
    )


def _message(entry_id: int, role: str, content: str, *, run_id: str) -> TapeEntry:
    return TapeEntry(
        id=entry_id,
        kind="message",
        payload={"role": role, "content": content},
        meta={"run_id": run_id},
        date="2026-03-10T00:00:00Z",
    )


def _tool_call(entry_id: int, name: str, *, run_id: str) -> TapeEntry:
    return TapeEntry(
        id=entry_id,
        kind="tool_call",
        payload={"calls": [{"function": {"name": name}}]},
        meta={"run_id": run_id},
        date="2026-03-10T00:00:00Z",
    )


def _tool_result(entry_id: int, result: str, *, run_id: str) -> TapeEntry:
    return TapeEntry(
        id=entry_id,
        kind="tool_result",
        payload={"results": [result]},
        meta={"run_id": run_id},
        date="2026-03-10T00:00:00Z",
    )
