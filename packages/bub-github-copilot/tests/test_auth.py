from __future__ import annotations

import pytest

from bub_github_copilot import auth


def test_token_path_uses_fixed_bub_location() -> None:
    assert auth.TOKEN_PATH == auth.Path.home() / ".bub" / "github_copilot_auth.json"


def test_resolve_github_copilot_token_prefers_persisted_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "github_copilot_auth.json")
    auth.save_github_copilot_oauth_tokens(
        auth.GitHubCopilotOAuthTokens(github_token="gho_saved")  # noqa: S106
    )

    assert auth.resolve_github_copilot_token() == "gho_saved"


def test_resolve_github_copilot_token_falls_back_to_gh_hosts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth,
        "TOKEN_PATH",
        tmp_path / "missing" / "github_copilot_auth.json",
    )
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    gh_dir = tmp_path / "gh"
    gh_dir.mkdir()
    (gh_dir / "hosts.yml").write_text(
        "github.com:\n  oauth_token: gho_from_hosts\n", encoding="utf-8"
    )

    assert auth.resolve_github_copilot_token(gh_config_dir=gh_dir) == "gho_from_hosts"


def test_login_github_copilot_oauth_persists_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(auth, "TOKEN_PATH", tmp_path / "github_copilot_auth.json")
    responses = iter(
        [
            {
                "device_code": "device-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "interval": 1,
                "expires_in": 900,
            },
            {
                "access_token": "gho_access",  # noqa: S106
                "token_type": "bearer",
                "scope": "read:user user:email",
            },
        ]
    )

    monkeypatch.setattr(auth, "_post_json", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(
        auth,
        "_fetch_profile",
        lambda *args, **kwargs: {
            "id": 7,
            "login": "octocat",
            "email": "octocat@example.com",
        },
    )

    notified: list[tuple[str, str]] = []
    tokens = auth.login_github_copilot_oauth(
        open_browser=False,
        device_code_notifier=lambda uri, code: notified.append((uri, code)),
    )

    assert notified == [("https://github.com/login/device", "ABCD-EFGH")]
    assert tokens.github_token == "gho_access"
    assert tokens.login == "octocat"
    persisted = auth.load_github_copilot_oauth_tokens()
    assert persisted is not None
    assert persisted.github_token == "gho_access"
