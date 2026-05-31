from __future__ import annotations

from pathlib import Path
from typing import Any

import bub
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import BaseScheduler
from bub import hookimpl
from bub.channels import Channel
from bub.framework import BubFramework
from bub.types import Envelope, MessageHandler, State

from bub_workflow.config import WorkflowSettings
from bub_workflow.constants import (
    WORKFLOW_SCHEDULER_STATE_KEY,
    WORKFLOW_SETTINGS_STATE_KEY,
    WORKFLOW_STATE_KEY,
    WORKFLOW_TAPE_STORE_STATE_KEY,
    WORKFLOW_TEMPLATES_STATE_KEY,
)


def default_scheduler() -> BaseScheduler:
    return AsyncIOScheduler()


class WorkflowImpl:
    def __init__(
        self,
        framework: BubFramework | None = None,
        templates: dict[str, dict[str, Any]] | None = None,
        settings: WorkflowSettings | None = None,
    ) -> None:
        from bub_workflow import tools  # noqa: F401

        self.framework = framework
        self.templates = templates or {}
        self.settings = settings or bub.ensure_config(WorkflowSettings)
        self.scheduler = default_scheduler()

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> State:
        del message, session_id
        state: State = {
            WORKFLOW_SCHEDULER_STATE_KEY: self.scheduler,
            WORKFLOW_SETTINGS_STATE_KEY: self.settings,
            WORKFLOW_STATE_KEY: {
                "tools": ["workflow.start", "workflow.step", "workflow.status"],
                "skill": "workflow",
            },
        }
        if self.framework is not None:
            tape_store = self.framework.get_tape_store()
            if tape_store is not None:
                state[WORKFLOW_TAPE_STORE_STATE_KEY] = tape_store
        if self.templates:
            state[WORKFLOW_TEMPLATES_STATE_KEY] = self.templates
        return state

    @hookimpl
    def save_state(
        self,
        session_id: str,
        state: State,
        message: Envelope,
        model_output: str,
    ) -> None:
        del session_id, message, model_output
        state.pop(WORKFLOW_SCHEDULER_STATE_KEY, None)
        state.pop(WORKFLOW_SETTINGS_STATE_KEY, None)
        state.pop(WORKFLOW_TAPE_STORE_STATE_KEY, None)

    @hookimpl
    def system_prompt(self, prompt: str | list[dict], state: State) -> str:
        del prompt
        if WORKFLOW_STATE_KEY not in state:
            return ""
        return _workflow_system_prompt()

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        del message_handler
        from bub_workflow.channel import WorkflowChannel

        return [WorkflowChannel(self.scheduler)]


def _workflow_system_prompt() -> str:
    skill_path = Path(__file__).resolve().parents[1] / "skills" / "workflow" / "SKILL.md"
    try:
        skill = skill_path.read_text(encoding="utf-8")
    except OSError:
        skill = ""
    return "\n".join(
        [
            "<workflow>",
            "The workflow tools are available: workflow.start, workflow.step, workflow.status.",
            (
                "Use the workflow skill when the user asks for tape-backed bee workflows, "
                "template-first task execution, bee checkpoints, or multi-agent review."
            ),
            "Use ordinary tools for linear work or a single small edit.",
            skill,
            "</workflow>",
        ]
    )
