from __future__ import annotations

from asyncio import Event

from bub.channels import Lifecycle
from bub.framework import BubFramework
from loguru import logger

from bub_dynamic_workflows.controller import WorkflowController


class WorkflowChannel(Lifecycle):
    name = "workflow"

    def __init__(self, framework: BubFramework) -> None:
        self.framework = framework
        self._controllers: dict[str, WorkflowController] = {}

    async def start(self, stop_event: Event) -> None:
        del stop_event
        logger.info("workflow.channel started")

    async def stop(self) -> None:
        for controller in list(self._controllers.values()):
            await controller.cancel_all()
        self._controllers.clear()
        logger.info("workflow.channel stopped")

    def bind_controller(self, session_id: str, controller: WorkflowController) -> None:
        self._controllers[session_id] = controller

    def controller(self, session_id: str) -> WorkflowController | None:
        return self._controllers.get(session_id)
