from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ECHO_PLUGIN_PATH = (
    PACKAGE_ROOT
    / "examples"
    / "echo-plugin"
    / "src"
    / "bub_ag_ui_example"
    / "plugin.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load example module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_echo_model_uses_bub_stream_events() -> None:
    plugin = _load_module("bub_ag_ui_example_echo", ECHO_PLUGIN_PATH)

    stream = await plugin.EchoModel().run_model_stream(
        prompt="hello from the example",
        session_id="example",
        state={},
    )
    events = [event async for event in stream]

    assert [event.kind for event in events] == ["text", "final"]
    assert events[0].data == {
        "delta": "Bub received through AG-UI: hello from the example"
    }
    assert events[1].data == {
        "text": "Bub received through AG-UI: hello from the example",
        "ok": True,
    }
