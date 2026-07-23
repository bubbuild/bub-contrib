from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = PACKAGE_ROOT / "examples" / "client.py"
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


def test_client_builds_protocol_native_request() -> None:
    client = _load_module("bub_ag_ui_example_client", CLIENT_PATH)

    input_data = client.build_run_input("hello from the example")
    payload = input_data.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert payload["threadId"].startswith("example-thread-")
    assert payload["runId"].startswith("example-run-")
    assert payload["state"] == {"example": "bub-ag-ui"}
    assert payload["messages"][0]["content"] == "hello from the example"
    assert payload["context"] == [
        {"description": "client", "value": "bub-ag-ui example"}
    ]


def test_client_decodes_ag_ui_sse_records() -> None:
    client = _load_module("bub_ag_ui_example_client_sse", CLIENT_PATH)
    lines = [
        b"event: message\n",
        b'data: {"type":"RUN_STARTED","runId":"run-1"}\n',
        b"\n",
        b'data: {"type":"RUN_FINISHED","runId":"run-1"}\n',
    ]

    events = list(client.decode_sse_events(lines))

    assert [event["type"] for event in events] == ["RUN_STARTED", "RUN_FINISHED"]


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
