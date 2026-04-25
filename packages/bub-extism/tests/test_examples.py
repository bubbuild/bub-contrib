from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bub_extism.bridge import ExtismBridge
from bub_extism.config import ExtismSettings
from bub_extism.plugin import ExtismPlugin

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUST_EXAMPLE = PACKAGE_ROOT / "examples" / "rust-model-stream"
GO_EXAMPLE = PACKAGE_ROOT / "examples" / "go-channel"


def _write_config(tmp_path: Path, body: dict[str, Any]) -> Path:
    config_path = tmp_path / "extism.json"
    config_path.write_text(json.dumps(body), encoding="utf-8")
    return config_path


def _plugin(config_path: Path) -> ExtismPlugin:
    plugin = ExtismPlugin(type("Framework", (), {})())
    plugin.bridge = ExtismBridge(ExtismSettings(config_path=config_path))
    return plugin


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
def test_rust_model_stream_example_builds_and_runs(tmp_path: Path) -> None:
    subprocess.run(
        ["cargo", "build", "--release", "--target", "wasm32-unknown-unknown"],
        cwd=RUST_EXAMPLE,
        check=True,
    )
    wasm_path = (
        RUST_EXAMPLE
        / "target"
        / "wasm32-unknown-unknown"
        / "release"
        / "bub_extism_rust_model_stream.wasm"
    )
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "rust",
            "plugins": {
                "rust": {
                    "wasmPath": str(wasm_path),
                    "hooks": {"run_model_stream": "run_model_stream"},
                }
            },
        },
    )

    async def run_example() -> list[tuple[str, dict[str, Any]]]:
        stream = await _plugin(config_path).bridge.call_hook(
            "run_model_stream",
            {
                "prompt": "hello from bub",
                "session_id": "example",
                "state": {},
            },
        )
        from bub_extism.stream import stream_events_from_value

        events = stream_events_from_value(stream)
        assert events is not None
        return [(event.kind, event.data) async for event in events]

    assert asyncio.run(run_example()) == [
        ("text", {"delta": "[rust-model-stream:example] hello from bub"}),
        ("final", {"text": "[rust-model-stream:example] hello from bub"}),
    ]


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_channel_example_builds_and_runs(tmp_path: Path) -> None:
    subprocess.run(["go", "mod", "tidy"], cwd=GO_EXAMPLE, check=True)
    wasm_path = tmp_path / "go-channel.wasm"
    subprocess.run(
        [
            "go",
            "build",
            "-buildmode=c-shared",
            "-o",
            str(wasm_path),
            ".",
        ],
        cwd=GO_EXAMPLE,
        check=True,
        env={**dict(os.environ), "GOOS": "wasip1", "GOARCH": "wasm"},
    )
    config_path = _write_config(
        tmp_path,
        {
            "defaultPlugin": "go",
            "plugins": {
                "go": {
                    "wasmPath": str(wasm_path),
                    "wasi": True,
                    "hooks": {"provide_channels": "provide_channels"},
                }
            },
        },
    )

    async def handler(message: dict[str, Any]) -> None:
        del message

    channels = _plugin(config_path).provide_channels(handler)
    assert [channel.name for channel in channels] == ["go-echo"]
    asyncio.run(channels[0].send({"content": "hello from bub"}))
