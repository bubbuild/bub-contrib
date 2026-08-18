from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bub_dsh import plugin


@pytest.fixture(autouse=True)
def close_dsh_runtimes():
    plugin._close_runtimes()
    yield
    plugin._close_runtimes()


class FakeRootSettings:
    def __init__(
        self,
        *,
        model: str = "dsh:deepseek-v4-flash",
        api_key: str | dict[str, str] | None = None,
        api_base: str | dict[str, str] | None = None,
        max_tokens: int = 16_384,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.max_tokens = max_tokens


def test_run_model_uses_dsh_sdk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, object] = {}

    class FakeHarness:
        def __init__(self, **kwargs: object) -> None:
            calls["kwargs"] = kwargs

        def close(self) -> None:
            calls["closed"] = True

        def run(self, prompt: str | list[dict], *, session_id: str):
            calls["run"] = {"prompt": prompt, "session_id": session_id}
            return type(
                "Result",
                (),
                {
                    "final_response": "assistant-output",
                    "finish_reason": "completed",
                    "events": [],
                },
            )()

    monkeypatch.setattr(plugin, "DeepSeekHarness", FakeHarness)
    dsh_settings = plugin.DshSettings(
        request_timeout_seconds=30,
        shutdown_timeout_seconds=2,
        session_root=tmp_path / "sessions",
    )
    root_settings = FakeRootSettings(
        api_key={"dsh": "secret", "openai": "other-secret"},
        api_base={
            "dsh": "https://deepseek.example/v1",
            "openai": "https://openai.example/v1",
        },
        max_tokens=8192,
    )
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda settings_type: (
            dsh_settings if settings_type is plugin.DshSettings else root_settings
        ),
    )

    result = asyncio.run(
        plugin.run_model(
            [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}],
            "telegram:42",
            {"_runtime_workspace": str(tmp_path)},
        )
    )

    assert result == "assistant-output"
    run_call = calls["run"]
    assert isinstance(run_call, dict)
    assert run_call["prompt"] == [
        {"type": "text", "text": "hello"},
        {"type": "text", "text": "world"},
    ]
    assert isinstance(run_call["session_id"], str)
    assert run_call["session_id"].startswith("bub-")
    assert calls["kwargs"] == {
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
        "cwd": str(tmp_path),
        "session_root": str(tmp_path / "sessions"),
        "request_timeout_seconds": 30.0,
        "shutdown_timeout_seconds": 2.0,
        "max_tokens": 8192,
        "base_url": "https://deepseek.example/v1",
        "api_key": "secret",
    }


def test_run_model_forwards_structured_prompt_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AQ=="}},
    ]
    captured: dict[str, object] = {}

    def fake_run_with_dsh(
        value: str | list[dict], *, session_id: str, workspace: Path
    ) -> str:
        captured.update(
            prompt=value,
            session_id=session_id,
            workspace=workspace,
        )
        return "assistant-output"

    monkeypatch.setattr(plugin, "_run_with_dsh", fake_run_with_dsh)

    result = asyncio.run(plugin.run_model(prompt, "session-1", {}))

    assert result == "assistant-output"
    assert captured["prompt"] is prompt
    assert captured["session_id"] == "session-1"


def test_run_model_exposes_harness_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness_error = {
        "message": "model failed",
        "code": "UNKNOWN",
        "retryable": False,
    }

    class FakeHarness:
        instances = 0

        def __init__(self, **kwargs: object) -> None:
            self.session_ids: list[str] = []
            FakeHarness.instances += 1

        def close(self) -> None:
            pass

        def run(self, prompt: str | list[dict], *, session_id: str):
            self.session_ids.append(session_id)
            return type(
                "Result",
                (),
                {
                    "final_response": "",
                    "finish_reason": "error",
                    "events": [
                        {
                            "type": "turn/end",
                            "data": {
                                "turn": 1,
                                "reason": {
                                    "kind": "error",
                                    "error": harness_error,
                                },
                            },
                        }
                    ],
                },
            )()

    monkeypatch.setattr(plugin, "DeepSeekHarness", FakeHarness)
    dsh_settings = plugin.DshSettings(session_root=tmp_path / "sessions")
    root_settings = FakeRootSettings()
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda settings_type: (
            dsh_settings if settings_type is plugin.DshSettings else root_settings
        ),
    )

    with pytest.raises(plugin.DshRunError) as exc_info:
        asyncio.run(plugin.run_model("hello", "session-error", {}))

    assert exc_info.value.error == harness_error
    assert str(exc_info.value) == "DeepSeek Harness run failed: model failed [UNKNOWN]"


def test_run_model_reuses_runtime_and_internal_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    instances = 0

    class FakeHarness:
        def __init__(self, **kwargs: object) -> None:
            nonlocal instances
            instances += 1

        def close(self) -> None:
            pass

        def run(self, prompt: str | list[dict], *, session_id: str):
            calls.append(session_id)
            return type(
                "Result",
                (),
                {
                    "final_response": "assistant-output",
                    "finish_reason": "completed",
                    "events": [],
                },
            )()

    monkeypatch.setattr(plugin, "DeepSeekHarness", FakeHarness)
    dsh_settings = plugin.DshSettings(session_root=tmp_path / "sessions")
    root_settings = FakeRootSettings()
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda settings_type: (
            dsh_settings if settings_type is plugin.DshSettings else root_settings
        ),
    )

    asyncio.run(plugin.run_model("first", "cli_session", {}))
    asyncio.run(plugin.run_model("second", "cli_session", {}))

    assert instances == 1
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0].startswith("bub-")


def test_run_model_forwards_internal_command_like_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_with_dsh(
        value: str | list[dict], *, session_id: str, workspace: Path
    ) -> str:
        captured["prompt"] = value
        return "assistant-output"

    monkeypatch.setattr(plugin, "_run_with_dsh", fake_run_with_dsh)

    result = asyncio.run(plugin.run_model(",help", "session-1", {}))

    assert result == "assistant-output"
    assert captured["prompt"] == ",help"


def test_harness_kwargs_omits_optional_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bub_home = tmp_path / "bub-home"
    monkeypatch.setattr(plugin.bub, "home", bub_home)
    dsh_settings = plugin.DshSettings()
    root_settings = FakeRootSettings(model="deepseek-v4-flash")
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda settings_type: (
            dsh_settings if settings_type is plugin.DshSettings else root_settings
        ),
    )

    kwargs = plugin._harness_kwargs(tmp_path / "workspace")

    assert kwargs["provider"] == "deepseek-official"
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["session_root"] == str(bub_home / "dsh")
    assert kwargs["max_tokens"] == 16_384
    assert "api_key" not in kwargs
    assert "base_url" not in kwargs


def test_harness_kwargs_forwards_other_root_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dsh_settings = plugin.DshSettings(session_root=tmp_path / "sessions")
    root_settings = FakeRootSettings(model="openai:gpt-5")
    monkeypatch.setattr(
        plugin.bub,
        "ensure_config",
        lambda settings_type: (
            dsh_settings if settings_type is plugin.DshSettings else root_settings
        ),
    )

    kwargs = plugin._harness_kwargs(tmp_path)

    assert kwargs["provider"] == "openai"
    assert kwargs["model"] == "gpt-5"
