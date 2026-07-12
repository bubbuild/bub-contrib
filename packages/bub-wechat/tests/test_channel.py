from pathlib import Path

from bub_wechat.channel import get_token_path


def test_token_path_uses_bub_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BUB_HOME", str(tmp_path))

    assert get_token_path() == tmp_path / "wechat_token.json"


def test_token_path_defaults_to_dot_bub(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BUB_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert get_token_path() == tmp_path / ".bub" / "wechat_token.json"
