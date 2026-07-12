from __future__ import annotations

from bub_slack.channel import _chunk_text, _extract_text


def test_extract_text_plain() -> None:
    assert _extract_text("hello") == "hello"


def test_extract_text_json_message() -> None:
    assert _extract_text('{"message": "hi"}') == "hi"


def test_extract_text_json_text_key() -> None:
    assert _extract_text('{"text": "yo"}') == "yo"


def test_extract_text_json_content_key() -> None:
    assert _extract_text('{"content": "c"}') == "c"


def test_extract_text_empty() -> None:
    assert _extract_text("") == ""


def test_chunk_text_small() -> None:
    assert _chunk_text("abc", 10) == ["abc"]


def test_chunk_text_exact_boundary() -> None:
    assert _chunk_text("abcd", 4) == ["abcd"]


def test_chunk_text_chunks_at_3900() -> None:
    text = "x" * 5000
    chunks = _chunk_text(text, 3900)
    assert len(chunks) == 2
    assert len(chunks[0]) == 3900
    assert len(chunks[1]) == 1100
    assert "".join(chunks) == text
