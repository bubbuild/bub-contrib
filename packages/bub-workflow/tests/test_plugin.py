from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from republic.tape.store import InMemoryTapeStore

from bub_workflow.config import WorkflowSettings
from bub_workflow.plugin import (
    WORKFLOW_SCHEDULER_STATE_KEY,
    WORKFLOW_SETTINGS_STATE_KEY,
    WORKFLOW_STATE_KEY,
    WORKFLOW_TAPE_STORE_STATE_KEY,
    WORKFLOW_TEMPLATES_STATE_KEY,
    WorkflowImpl,
)


def test_plugin_load_state_exposes_scheduler_tape_store_and_workflow_metadata() -> None:
    tape_store = InMemoryTapeStore()
    framework = SimpleNamespace(get_tape_store=lambda: tape_store)
    settings = WorkflowSettings()
    plugin = WorkflowImpl(framework, settings=settings)  # type: ignore[arg-type]

    state = plugin.load_state({}, "cli:default")

    assert state[WORKFLOW_SCHEDULER_STATE_KEY] is plugin.scheduler
    assert state[WORKFLOW_SETTINGS_STATE_KEY] is settings
    assert state[WORKFLOW_TAPE_STORE_STATE_KEY] is tape_store
    assert state[WORKFLOW_STATE_KEY] == {
        "tools": ["workflow.start", "workflow.step", "workflow.status"],
        "skill": "workflow",
    }


def test_plugin_load_state_exposes_template_registry_when_configured() -> None:
    templates = {
        "repo_review": {
            "name": "repo_review",
            "nodes": [
                {
                    "id": "inventory",
                    "executor": "function",
                    "call": "tests.fake:inventory",
                }
            ],
        }
    }
    plugin = WorkflowImpl(templates=templates)

    state = plugin.load_state({}, "cli:default")

    assert state[WORKFLOW_TEMPLATES_STATE_KEY] is templates


def test_workflow_settings_read_environment(monkeypatch) -> None:
    monkeypatch.setenv("BUB_WORKFLOW_PROJECTION_DIR", "state/workflows")
    monkeypatch.setenv(
        "BUB_WORKFLOW_TEMPLATE_DIRS",
        '["config/templates", "/opt/bub/workflow/templates"]',
    )

    settings = WorkflowSettings()

    assert settings.projection_dir == Path("state/workflows")
    assert settings.template_dirs == [
        Path("config/templates"),
        Path("/opt/bub/workflow/templates"),
    ]


def test_plugin_system_prompt_includes_workflow_skill_guidance() -> None:
    plugin = WorkflowImpl()
    state = {WORKFLOW_STATE_KEY: {"skill": "workflow"}}

    prompt = plugin.system_prompt("review this repo", state)

    assert "<workflow>" in prompt
    assert "workflow.start" in prompt
    assert "workflow.step" in prompt
    assert "tapexbee" in prompt
    assert "bee topic" in prompt


def test_plugin_save_state_removes_runtime_only_values() -> None:
    plugin = WorkflowImpl()
    state = {
        WORKFLOW_SCHEDULER_STATE_KEY: object(),
        WORKFLOW_SETTINGS_STATE_KEY: object(),
        WORKFLOW_TAPE_STORE_STATE_KEY: object(),
        WORKFLOW_STATE_KEY: {"skill": "workflow"},
    }

    plugin.save_state("cli:default", state, {}, "done")

    assert WORKFLOW_SCHEDULER_STATE_KEY not in state
    assert WORKFLOW_SETTINGS_STATE_KEY not in state
    assert WORKFLOW_TAPE_STORE_STATE_KEY not in state
    assert state[WORKFLOW_STATE_KEY] == {"skill": "workflow"}
