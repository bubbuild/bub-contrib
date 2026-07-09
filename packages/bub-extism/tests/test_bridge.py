from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pluggy
import pytest
from republic import TapeEntry, TapeQuery

from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs
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
def fake_extism(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePlugin.calls = []
    FakePlugin.exports = {}
    monkeypatch.setitem(sys.modules, "extism", SimpleNamespace(Plugin=FakePlugin))


def _write_config(tmp_path: Path, body: dict[str, Any]) -> Path:
    config_path = tmp_path / "extism.json"
    config_path.write_text(json.dumps(body), encoding="utf-8")
    return config_path


def _runtime(config_path: Path) -> HookRuntime:
    settings = ExtismSettings(config_path=config_path)
    plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
    plugin_manager.add_hookspecs(BubHookSpecs)
    framework = SimpleNamespace(_plugin_manager=plugin_manager)
    plugin = ExtismPlugin(framework, settings=settings)
    plugin_manager.register(plugin, name="extism")
    return HookRuntime(plugin_manager)


def _flatten_channel_results(results: list[list[Any]]) -> list[Any]:
    channels: list[Any] = []
    for batch in results:
        channels.extend(batch)
    return channels


def test_runtime_registers_configured_hook_adapters(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "prompt": {
                    "manifest": {"wasm": [{"path": "./prompt.wasm"}]},
                    "hooks": {"build_prompt": "build_prompt"},
                },
                "model": {
                    "manifest": {"wasm": [{"path": "./model.wasm"}]},
                    "hooks": {"run_model": "run_model"},
                },
            }
        },
    )

    report = _runtime(config_path).hook_report()

    assert report["build_prompt"] == ["extism:prompt"]
    assert report["run_model"] == ["extism:model"]


def test_run_model_calls_configured_export_with_unified_request(
    tmp_path: Path,
) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "echo": {
                    "manifest": {
                        "wasm": [{"path": "./plugin.wasm", "hash": "demo"}],
                        "config": {"model": "demo"},
                    },
                    "wasi": True,
                    "hooks": {"run_model": "bub_run_model"},
                }
            }
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
        _runtime(config_path).run_model(
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
            "plugin_input": {
                "wasm": [{"path": "./plugin.wasm", "hash": "demo"}],
                "config": {"model": "demo"},
            },
            "wasi": True,
            "config": None,
        }
    ]


def test_system_prompt_accepts_plain_text_result(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "prompt": {
                    "manifest": {"wasm": [{"url": "https://example.com/prompt.wasm"}]},
                    "hooks": {"system_prompt": "system_prompt"},
                }
            }
        },
    )
    FakePlugin.exports = {"system_prompt": b"from wasm"}

    result = _runtime(config_path).call_first_sync(
        "system_prompt",
        prompt="hello",
        state={"session_id": "s1"},
    )

    assert result == "from wasm"
    assert FakePlugin.calls[0]["plugin_input"] == {
        "wasm": [{"url": "https://example.com/prompt.wasm"}]
    }


def test_missing_export_skips_hook(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "missing": {
                    "manifest": {"wasm": [{"url": "https://example.com/plugin.wasm"}]},
                    "hooks": {"run_model": "missing_run_model"},
                }
            }
        },
    )

    result = asyncio.run(
        _runtime(config_path).run_model(
            prompt="hello",
            session_id="s1",
            state={},
        )
    )

    assert result is None
    assert FakePlugin.calls == []


def test_run_model_stream_wraps_returned_events(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "stream": {
                    "manifest": {"wasm": [{"url": "https://example.com/stream.wasm"}]},
                    "hooks": {"run_model_stream": "run_model_stream"},
                }
            }
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
        _runtime(config_path).run_model_stream(
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


def test_run_model_stream_rejects_invalid_usage_shape(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "stream": {
                    "manifest": {"wasm": [{"url": "https://example.com/stream.wasm"}]},
                    "hooks": {"run_model_stream": "run_model_stream"},
                }
            }
        },
    )
    FakePlugin.exports = {
        "run_model_stream": json.dumps(
            {
                "value": {
                    "events": [],
                    "usage": "invalid",
                }
            }
        )
    }

    with pytest.raises(RuntimeError, match="usage must be a JSON object"):
        asyncio.run(
            _runtime(config_path).run_model_stream(
                prompt="hello",
                session_id="s1",
                state={},
            )
        )


def test_build_prompt_and_run_model_can_be_split_across_plugins(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "prompt": {
                    "manifest": {"wasm": [{"path": "./prompt.wasm"}]},
                    "hooks": {"build_prompt": "build_prompt"},
                },
                "model": {
                    "manifest": {"wasm": [{"path": "./model.wasm"}]},
                    "hooks": {"run_model": "run_model"},
                },
            }
        },
    )
    FakePlugin.exports = {
        "build_prompt": lambda request: json.dumps(
            {
                "value": (
                    f"[prompt:{request['args']['session_id']}] "
                    f"{request['args']['message']['content']}"
                )
            }
        ),
        "run_model": lambda request: json.dumps(
            {
                "value": (
                    f"[model:{request['args']['session_id']}] "
                    f"{request['args']['prompt']}"
                )
            }
        ),
    }

    runtime = _runtime(config_path)
    prompt = asyncio.run(
        runtime.call_first(
            "build_prompt",
            message={"content": "hello from bub"},
            session_id="example",
            state={},
        )
    )
    output = asyncio.run(
        runtime.run_model(
            prompt=prompt,
            session_id="example",
            state={},
        )
    )

    assert prompt == "[prompt:example] hello from bub"
    assert output == "[model:example] [prompt:example] hello from bub"
    assert [call["function_name"] for call in FakePlugin.calls] == [
        "build_prompt",
        "run_model",
    ]


async def _collect_stream(stream):
    return [event async for event in stream]


def test_tape_store_proxy_forwards_operations(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "tape": {
                    "manifest": {"wasm": [{"url": "https://example.com/tape.wasm"}]},
                    "hooks": {"provide_tape_store": "provide_tape_store"},
                }
            }
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

    store = _runtime(config_path).call_first_sync("provide_tape_store")

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


def test_tape_store_rejects_invalid_entry_shape(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "tape": {
                    "manifest": {"wasm": [{"url": "https://example.com/tape.wasm"}]},
                    "hooks": {"provide_tape_store": "provide_tape_store"},
                }
            }
        },
    )
    FakePlugin.exports = {
        "provide_tape_store": json.dumps(
            {
                "value": {
                    "functions": {
                        "fetch_all": "fetch_all",
                    }
                }
            }
        ),
        "fetch_all": json.dumps({"value": ["bad-entry"]}),
    }

    store = _runtime(config_path).call_first_sync("provide_tape_store")

    assert store is not None
    with pytest.raises(RuntimeError, match="tape entry must be an object"):
        list(store.fetch_all(TapeQuery("main", store)))


def test_tape_store_rejects_invalid_functions_shape(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "tape": {
                    "manifest": {"wasm": [{"url": "https://example.com/tape.wasm"}]},
                    "hooks": {"provide_tape_store": "provide_tape_store"},
                }
            }
        },
    )
    FakePlugin.exports = {
        "provide_tape_store": json.dumps(
            {
                "value": {
                    "functions": ["fetch_all"],
                }
            }
        )
    }

    with pytest.raises(RuntimeError, match="functions object"):
        _runtime(config_path).call_first_sync("provide_tape_store")


def test_tape_store_requires_functions_object(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "tape": {
                    "manifest": {"wasm": [{"url": "https://example.com/tape.wasm"}]},
                    "hooks": {"provide_tape_store": "provide_tape_store"},
                }
            }
        },
    )
    FakePlugin.exports = {
        "provide_tape_store": json.dumps({"value": {}}),
    }

    with pytest.raises(RuntimeError, match="functions object"):
        _runtime(config_path).call_first_sync("provide_tape_store")


def test_channel_proxy_forwards_send(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "channel": {
                    "manifest": {"wasm": [{"url": "https://example.com/channel.wasm"}]},
                    "hooks": {"provide_channels": "provide_channels"},
                }
            }
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

    channel_batches = _runtime(config_path).call_many_sync(
        "provide_channels",
        message_handler=handler,
    )
    channels = _flatten_channel_results(channel_batches)

    assert [channel.name for channel in channels] == ["wasm"]
    asyncio.run(channels[0].send({"content": "hello"}))
    assert FakePlugin.calls[-1]["function_name"] == "channel_send"
    assert FakePlugin.calls[-1]["payload"]["hook"] == "channel.send"


def test_channel_proxy_rejects_invalid_wrapper_shape(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "channel": {
                    "manifest": {"wasm": [{"url": "https://example.com/channel.wasm"}]},
                    "hooks": {"provide_channels": "provide_channels"},
                }
            }
        },
    )
    FakePlugin.exports = {
        "provide_channels": json.dumps({"value": {"channels": "bad"}}),
    }

    async def handler(message: dict[str, Any]) -> None:
        del message

    with pytest.raises(RuntimeError, match="list of channel descriptors"):
        _runtime(config_path).call_many_sync(
            "provide_channels", message_handler=handler
        )
