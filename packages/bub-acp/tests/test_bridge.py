from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bub_acp.bridge import ACPBridge
from bub_acp.config import ACPAgentProcessConfig, ACPSettings


def _settings(tmp_path: Path) -> ACPSettings:
    settings = ACPSettings(config_path=tmp_path / "acp.json")
    settings.upsert_agent(
        "fake",
        ACPAgentProcessConfig(
            command=sys.executable,
            args=[str(Path(__file__).parent / "fixtures" / "fake_acp_agent.py")],
        ),
        make_default=True,
    )
    return settings


@pytest.mark.asyncio
async def test_bridge_streams_real_acp_process(tmp_path: Path) -> None:
    bridge = ACPBridge(_settings(tmp_path))
    state = {"_runtime_workspace": str(tmp_path)}

    stream = await bridge.run_model_stream(
        "permission filesystem terminal",
        session_id="session-1",
        state=state,
    )

    assert stream is not None

    events = [event async for event in stream]
    kinds = [event.kind for event in events]
    assert "text" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "usage" in kinds
    assert events[-1].kind == "final"
    assert "turn=1" in events[-1].data["text"]
    assert "permission=selected" in events[-1].data["text"]
    assert "file=hello from fake agent" in events[-1].data["text"]
    assert "terminal=terminal ok" in events[-1].data["text"]
    assert (tmp_path / "notes" / "output.txt").read_text(encoding="utf-8").strip() == "hello from fake agent"


@pytest.mark.asyncio
async def test_bridge_reuses_remote_session_mapping(tmp_path: Path) -> None:
    bridge = ACPBridge(_settings(tmp_path))
    state = {"_runtime_workspace": str(tmp_path)}

    first = await bridge.run_model("filesystem", session_id="session-2", state=state)
    second = await bridge.run_model("filesystem", session_id="session-2", state=state)

    assert first is not None
    assert second is not None
    assert "turn=1" in first
    assert "turn=2" in second


@pytest.mark.asyncio
async def test_bridge_internal_command_uses_runtime_agent(tmp_path: Path) -> None:
    class FakeRuntimeAgent:
        async def run(self, *, session_id: str, prompt: str, state: dict[str, str]) -> str:
            return f"internal:{prompt}"

    bridge = ACPBridge(_settings(tmp_path))
    state = {
        "_runtime_workspace": str(tmp_path),
        "_runtime_agent": FakeRuntimeAgent(),
    }

    result = await bridge.run_model(",help", session_id="session-3", state=state)

    assert result == "internal:,help"
