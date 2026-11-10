from __future__ import annotations

from pathlib import Path


def test_qq_skill_returns_text_instead_of_script_send() -> None:
    skill = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("src", "skills", "qq", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    assert "QQChannel.send" in skill
    assert "uv run python ./scripts/qq_send.py" not in skill
    assert "Do not construct or pass `msg_seq`." in skill
    assert "do not call `qq.send` with `<no_reply/>`" in skill
    assert "tape.search" not in skill
    assert "outbox" in skill
    assert "media_path" in skill
    assert "bash" in skill
