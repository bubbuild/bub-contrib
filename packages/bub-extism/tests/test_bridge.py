from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pluggy
from republic import TapeEntry, TapeQuery

from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs
from bub_extism.bridge import ExtismBridge
from bub_extism.config import ExtismSettings
from bub_extism.plugin import ExtismPlugin


class FakePlugin:
    calls: list[dict[str, Any]] = []
    exports: dict[str, Any] = {}

    def __init__(
        self,
        plugin_input: dict[str, Any] | bytes,
        *,
        wasi: bool = False,
        config: dict[str, str] | None = None,
    ) -> None:
        self.plugin_input = plugin_input
        self.wasi = wasi
        self.config = config

    def __enter__(self) -> FakePlugin:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def function_exists(self, name: str) -> bool:
        return name in self.exports

    def call(self, function_name: str, data: str) -> Any:
        payload = json.loads(data)
        self.calls.append(
            {
                "function_name": function_name,
                "payload": payload,
                "plugin_input": self.plugin_input,
                "wasi": self.wasi,
                "config": self.config,
            }
        )
        result = self.exports[function_name]
        if callable(result):
            return result(payload)
        return result


@pytest.fixture(autouse=True)
def fake_extism(monkeypatch):
    FakePlugin.calls = []
    FakePlugin.exports = {}
    monkeypatch.setitem(sys.modules, "extism", SimpleNamespace(Plugin=FakePlugin))


def _write_config(tmp_path: Path, body: dict[str, Any]) -> Path:
    config_path = tmp_path / "extism.json"
    config_path.write_text(json.dumps(body), encoding="utf-8")
    return config_path


def _bridge(config_path: Path) -> ExtismBridge:
    return ExtismBridge(ExtismSettings(config_path=config_path))


def _plugin(config_path: Path) -> ExtismPlugin:
    plugin = ExtismPlugin(SimpleNamespace())
    plugin.bridge = _bridge(config_path)
    return plugin


def _runtime(config_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookRuntime:
    monkeypatch.setenv("BUB_EXTISM_CONFIG_PATH", str(config_path))
    plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
    plugin_manager.add_hookspecs(BubHookSpecs)
    framework = SimpleNamespace(_plugin_manager=plugin_manager)
    plugin = ExtismPlugin(framework)
    plugin_manager.register(plugin, name="extism")
    return HookRuntime(plugin_manager)


def test_plugin_exposes_all_non_model_standard_bub_hooks() -> None:
    expected_hooks = {
        "resolve_session",
        "build_prompt",
        "load_state",
        "save_state",
        "render_outbound",
        "dispatch_outbound",
        "register_cli_commands",
        "onboard_config",
        "on_error",
        "system_prompt",
        "provide_tape_store",
        "provide_channels",
        "build_tape_context",
    }

    assert expected_hooks <= set(dir(ExtismPlugin))


def test_model_hook_adapter_registers_only_one_model_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "model",
            "plugins": {
                "model": {
                    "wasmUrl": "https://example.com/model.wasm",
                    "hooks": {
                        "run_model": "run_model",
                        "run_model_stream": "run_model_stream",
                    },
                }
            },
        },
    )

    runtime = _runtime(config_path, monkeypatch)

    report = runtime.hook_report()
    assert report["run_model_stream"] == ["extism-run-model-stream"]
    assert "run_model" not in report


def test_call_hook_returns_none_without_selected_plugin(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path, {"plugins": {}})

    result = _bridge(config_path).call_hook_sync("run_model", {"prompt": "hello"})

    assert result is None
    assert FakePlugin.calls == []


def test_run_model_calls_configured_export_with_unified_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wasm_path = tmp_path / "plugin.wasm"
    wasm_path.write_bytes(b"\0asm")
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "echo",
            "plugins": {
                "echo": {
                    "wasmPath": str(wasm_path),
                    "wasi": True,
                    "config": {"model": "demo"},
                    "hooks": {"run_model": "bub_run_model"},
                }
            },
        },
    )
    FakePlugin.exports = {
        "bub_run_model": lambda request: json.dumps(
            {
                "value": (
                    f"echo:{request['args']['session_id']}:"
                    f"{request['args']['prompt']}:"
                    f"{sorted(request['args']['state'])}"
                )
            }
        )
    }

    result = asyncio.run(
        _runtime(config_path, monkeypatch).run_model(
            prompt="hello",
            session_id="s1",
            state={
                "visible": {"ok": True},
                "_runtime_agent": object(),
                "not_json": object(),
            },
        )
    )

    assert result == "echo:s1:hello:['visible']"
    assert FakePlugin.calls == [
        {
            "function_name": "bub_run_model",
            "payload": {
                "abi_version": "bub.extism.v1",
                "hook": "run_model",
                "args": {
                    "prompt": "hello",
                    "session_id": "s1",
                    "state": {"visible": {"ok": True}},
                },
            },
            "plugin_input": b"\0asm",
            "wasi": True,
            "config": {"model": "demo"},
        }
    ]


def test_system_prompt_accepts_plain_text_result(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "prompt",
            "plugins": {
                "prompt": {
                    "wasmUrl": "https://example.com/prompt.wasm",
                    "hooks": {"system_prompt": "system_prompt"},
                }
            },
        },
    )
    FakePlugin.exports = {"system_prompt": b"from wasm"}

    result = _plugin(config_path).system_prompt("hello", {"session_id": "s1"})

    assert result == "from wasm"
    assert FakePlugin.calls[0]["plugin_input"] == {
        "wasm": [{"url": "https://example.com/prompt.wasm"}]
    }


def test_missing_export_skips_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "missing",
            "plugins": {
                "missing": {
                    "manifest": {"wasm": [{"url": "https://example.com/plugin.wasm"}]},
                    "hooks": {"run_model": "missing_run_model"},
                }
            },
        },
    )

    result = asyncio.run(
        _runtime(config_path, monkeypatch).run_model(
            prompt="hello",
            session_id="s1",
            state={},
        )
    )

    assert result is None
    assert FakePlugin.calls == []


def test_run_model_stream_wraps_returned_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "stream",
            "plugins": {
                "stream": {
                    "wasmUrl": "https://example.com/stream.wasm",
                    "hooks": {"run_model_stream": "run_model_stream"},
                }
            },
        },
    )
    FakePlugin.exports = {
        "run_model_stream": json.dumps(
            {
                "value": {
                    "events": [
                        {"kind": "text", "data": {"delta": "hello"}},
                        {"kind": "final", "data": {"text": "hello"}},
                    ],
                    "usage": {"output_tokens": 1},
                }
            }
        )
    }

    stream = asyncio.run(
        _runtime(config_path, monkeypatch).run_model_stream(
            prompt="hello",
            session_id="s1",
            state={},
        )
    )
    assert stream is not None
    events = asyncio.run(_collect_stream(stream))
    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": "hello"}),
        ("final", {"text": "hello"}),
    ]
    assert stream.usage == {"output_tokens": 1}


async def _collect_stream(stream):
    return [event async for event in stream]


def test_tape_store_proxy_forwards_operations(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "tape",
            "plugins": {
                "tape": {
                    "wasmUrl": "https://example.com/tape.wasm",
                    "hooks": {"provide_tape_store": "provide_tape_store"},
                }
            },
        },
    )
    FakePlugin.exports = {
        "provide_tape_store": json.dumps(
            {
                "value": {
                    "functions": {
                        "list_tapes": "list_tapes",
                        "fetch_all": "fetch_all",
                        "append": "append",
                        "reset": "reset",
                    }
                }
            }
        ),
        "list_tapes": json.dumps({"value": ["main"]}),
        "fetch_all": json.dumps(
            {
                "value": [
                    {
                        "id": 1,
                        "kind": "message",
                        "payload": {"role": "user", "content": "hello"},
                        "meta": {},
                        "date": "2026-04-26T00:00:00+00:00",
                    }
                ]
            }
        ),
        "append": json.dumps({"skip": True}),
        "reset": json.dumps({"skip": True}),
    }

    store = _plugin(config_path).provide_tape_store()

    assert store is not None
    assert store.list_tapes() == ["main"]
    entries = list(store.fetch_all(TapeQuery("main", store)))
    assert entries == [
        TapeEntry(
            id=1,
            kind="message",
            payload={"role": "user", "content": "hello"},
            meta={},
            date="2026-04-26T00:00:00+00:00",
        )
    ]
    store.append("main", TapeEntry.message({"role": "assistant", "content": "ok"}))
    store.reset("main")


def test_channel_proxy_forwards_send(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "channel",
            "plugins": {
                "channel": {
                    "wasmUrl": "https://example.com/channel.wasm",
                    "hooks": {"provide_channels": "provide_channels"},
                }
            },
        },
    )
    FakePlugin.exports = {
        "provide_channels": json.dumps(
            {
                "value": [
                    {
                        "name": "wasm",
                        "functions": {
                            "send": "channel_send",
                        },
                    }
                ]
            }
        ),
        "channel_send": json.dumps({"value": True}),
    }

    async def handler(message: dict[str, Any]) -> None:
        del message

    channels = _plugin(config_path).provide_channels(handler)

    assert [channel.name for channel in channels] == ["wasm"]
    asyncio.run(channels[0].send({"content": "hello"}))
    assert FakePlugin.calls[-1]["function_name"] == "channel_send"
    assert FakePlugin.calls[-1]["payload"]["hook"] == "channel.send"
