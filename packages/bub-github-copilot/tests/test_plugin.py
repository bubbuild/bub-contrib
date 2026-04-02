from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bub.builtin.auth import app as auth_app
from bub_github_copilot import plugin


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def run(
        self,
        *,
        session_id: str,
        prompt: str,
        state: dict[str, object],
    ) -> str:
        self.calls.append((session_id, prompt, state))
        return "internal-command-result"


def test_run_model_delegates_internal_commands_to_runtime_agent() -> None:
    state: dict[str, object] = {"_runtime_agent": FakeAgent()}

    result = asyncio.run(plugin.run_model(",help", session_id="session-1", state=state))

    agent = state["_runtime_agent"]
    assert result == "internal-command-result"
    assert isinstance(agent, FakeAgent)
    assert agent.calls == [("session-1", ",help", state)]


def test_run_model_uses_copilot_sdk_with_prompt_and_attachments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    class FakeSession:
        async def send_and_wait(self, prompt, *, attachments=None, timeout=None):
            calls["send"] = {
                "prompt": prompt,
                "attachments": attachments,
                "timeout": timeout,
            }
            return type(
                "AssistantMessage",
                (),
                {"data": type("Data", (), {"content": "assistant-output"})()},
            )()

        def get_messages(self):
            return []

        async def disconnect(self):
            calls["disconnected"] = True

    class FakeClient:
        def __init__(self, config) -> None:
            calls["config"] = config

        async def start(self):
            calls["started"] = True

        async def stop(self):
            calls["stopped"] = True

        async def resume_session(self, session_id, **kwargs):
            calls["resume"] = {"session_id": session_id, "kwargs": kwargs}
            raise RuntimeError("missing session")

        async def create_session(self, **kwargs):
            calls["create"] = kwargs
            return FakeSession()

    monkeypatch.setattr(plugin, "CopilotClient", FakeClient)
    monkeypatch.setattr(
        plugin, "resolve_github_copilot_token", lambda *_args, **_kwargs: "gho_test"
    )
    monkeypatch.setattr(
        plugin,
        "github_copilot_settings",
        plugin.GitHubCopilotSettings(
            model="gpt-5",
            reasoning_effort="high",
            timeout_seconds=12.0,
            log_level="debug",
        ),
    )

    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    prompt = [
        {"type": "text", "text": "hello from bub"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]
    state = {"_runtime_workspace": str(tmp_path)}

    result = asyncio.run(
        plugin.run_model(prompt, session_id="telegram:42", state=state)
    )

    assert result == "assistant-output"
    assert calls["started"] is True
    assert calls["stopped"] is True
    assert calls["disconnected"] is True
    create_kwargs = calls["create"]
    assert isinstance(create_kwargs, dict)
    assert create_kwargs["session_id"].startswith("bub-telegram-42-")
    assert create_kwargs["model"] == "gpt-5"
    assert create_kwargs["reasoning_effort"] == "high"
    assert create_kwargs["working_directory"] == str(tmp_path)
    assert create_kwargs["config_dir"] == str(tmp_path / ".bub-github-copilot")
    assert create_kwargs["skill_directories"] == [str(skills_dir)]
    send_kwargs = calls["send"]
    assert isinstance(send_kwargs, dict)
    assert send_kwargs["prompt"] == "hello from bub"
    assert send_kwargs["attachments"] == [
        {
            "type": "blob",
            "data": "aGVsbG8=",
            "mimeType": "image/png",
            "displayName": "attachment-1",
        }
    ]
    assert send_kwargs["timeout"] == 12.0
    config = calls["config"]
    assert config.github_token == "gho_test"
    assert config.cwd == str(tmp_path)
    assert config.log_level == "debug"


def test_run_model_raises_when_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        plugin, "resolve_github_copilot_token", lambda *_args, **_kwargs: None
    )

    with pytest.raises(RuntimeError, match="No GitHub token found"):
        asyncio.run(plugin.run_model("hello", session_id="session-1", state={}))


def test_github_login_command_runs_oauth_flow_and_prints_usage_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_login_github_copilot_oauth(
        **kwargs: object,
    ) -> plugin.GitHubCopilotOAuthTokens:
        captured.update(kwargs)
        notifier = kwargs["device_code_notifier"]
        assert callable(notifier)
        notifier("https://github.com/login/device", "ABCD-EFGH")
        return plugin.GitHubCopilotOAuthTokens(
            github_token="github-token",  # noqa: S106
            account_id="12345",
            login="octocat",
        )

    monkeypatch.setattr(
        plugin, "login_github_copilot_oauth", fake_login_github_copilot_oauth
    )

    result = CliRunner().invoke(
        auth_app,
        ["github", "--no-browser"],
    )

    assert result.exit_code == 0
    assert captured["open_browser"] is False
    assert captured["timeout_seconds"] == 300.0
    assert "https://github.com/login/device" in result.stdout
    assert "Enter code: ABCD-EFGH" in result.stdout
    assert "login: ok" in result.stdout
    assert "login_name: octocat" in result.stdout
    assert "account_id: 12345" in result.stdout
    assert f"auth_file: {plugin.TOKEN_PATH}" in result.stdout
    assert "BUB_GITHUB_COPILOT_MODEL=gpt-5" in result.stdout


def test_github_copilot_login_alias_uses_same_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_login_github_copilot_oauth(
        **kwargs: object,
    ) -> plugin.GitHubCopilotOAuthTokens:
        captured.update(kwargs)
        return plugin.GitHubCopilotOAuthTokens(github_token="github-token")  # noqa: S106

    monkeypatch.setattr(
        plugin, "login_github_copilot_oauth", fake_login_github_copilot_oauth
    )

    result = CliRunner().invoke(
        auth_app,
        ["github-copilot"],
    )

    assert result.exit_code == 0
    assert captured["open_browser"] is True
