from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pluggy
import pytest

from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs
from bub_extism.config import ExtismSettings
from bub_extism.plugin import ExtismPlugin

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RUST_EXAMPLE = PACKAGE_ROOT / "examples" / "rust-run-model"
GO_EXAMPLE = PACKAGE_ROOT / "examples" / "go-build-prompt"


def _has_rust_wasm_target() -> bool:
    if shutil.which("cargo") is None or shutil.which("rustup") is None:
        return False
    result = subprocess.run(
        ["rustup", "target", "list", "--installed"],
        check=False,
        capture_output=True,
        text=True,
    )
    return "wasm32-unknown-unknown" in result.stdout.split()


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


def _build_rust_example() -> Path:
    subprocess.run(
        ["cargo", "build", "--release", "--target", "wasm32-unknown-unknown"],
        cwd=RUST_EXAMPLE,
        check=True,
    )
    return (
        RUST_EXAMPLE
        / "target"
        / "wasm32-unknown-unknown"
        / "release"
        / "bub_extism_rust_run_model.wasm"
    )


def _build_go_example(tmp_path: Path) -> Path:
    subprocess.run(["go", "mod", "tidy"], cwd=GO_EXAMPLE, check=True)
    wasm_path = tmp_path / "go-build-prompt.wasm"
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
    return wasm_path


@pytest.mark.skipif(not _has_rust_wasm_target(), reason="cargo or wasm32-unknown-unknown target is not installed")
def test_rust_run_model_example_builds_and_runs(tmp_path: Path) -> None:
    wasm_path = _build_rust_example()
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "rust": {
                    "manifest": {"wasm": [{"path": str(wasm_path)}]},
                    "hooks": {"run_model": "run_model"},
                }
            }
        },
    )

    result = asyncio.run(
        _runtime(config_path).run_model(
            prompt="hello from bub",
            session_id="example",
            state={},
        )
    )

    assert result == "[rust-run-model:example] hello from bub"


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_build_prompt_example_builds_and_runs(tmp_path: Path) -> None:
    wasm_path = _build_go_example(tmp_path)
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "go": {
                    "manifest": {"wasm": [{"path": str(wasm_path)}]},
                    "wasi": True,
                    "hooks": {"build_prompt": "build_prompt"},
                }
            }
        },
    )

    prompt = asyncio.run(
        _runtime(config_path).call_first(
            "build_prompt",
            message={"content": "hello from bub"},
            session_id="example",
            state={},
        )
    )

    assert prompt == "[go-build-prompt:example] hello from bub"


@pytest.mark.skipif(
    not _has_rust_wasm_target() or shutil.which("go") is None,
    reason="cargo target or go is not installed",
)
def test_go_and_rust_examples_can_be_combined(tmp_path: Path) -> None:
    rust_wasm_path = _build_rust_example()
    go_wasm_path = _build_go_example(tmp_path)
    config_path = _write_config(
        tmp_path,
        {
            "plugins": {
                "prompt": {
                    "manifest": {"wasm": [{"path": str(go_wasm_path)}]},
                    "wasi": True,
                    "hooks": {"build_prompt": "build_prompt"},
                },
                "model": {
                    "manifest": {"wasm": [{"path": str(rust_wasm_path)}]},
                    "hooks": {"run_model": "run_model"},
                },
            }
        },
    )

    runtime = _runtime(config_path)
    prompt = asyncio.run(
        runtime.call_first(
            "build_prompt",
            message={"content": "hello from bub"},
            session_id="example",
            state={},
        )
    )
    result = asyncio.run(
        runtime.run_model(
            prompt=prompt,
            session_id="example",
            state={},
        )
    )

    assert prompt == "[go-build-prompt:example] hello from bub"
    assert result == "[rust-run-model:example] [go-build-prompt:example] hello from bub"
