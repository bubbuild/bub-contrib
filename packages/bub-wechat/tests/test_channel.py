from pathlib import Path

import bub

from bub_wechat.channel import get_token_path


def test_token_path_uses_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bub, "home", tmp_path)

    assert get_token_path() == tmp_path / "wechat_token.json"
