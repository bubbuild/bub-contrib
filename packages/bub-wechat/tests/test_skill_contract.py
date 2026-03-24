from __future__ import annotations

from pathlib import Path


def test_wechat_skill_returns_text_instead_of_tool_send() -> None:
    skill = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("src", "skills", "wechat", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    assert "WeChatChannel.send" in skill
    assert "Do not call any active-send tool for normal replies." in skill
