from __future__ import annotations

import asyncio
from asyncio import Event

from apscheduler.schedulers.base import BaseScheduler
from bub.channels import Lifecycle
from loguru import logger


class WorkflowChannel(Lifecycle):
    name = "workflow"

    def __init__(self, scheduler: BaseScheduler) -> None:
        self.scheduler = scheduler

    async def start(self, stop_event: Event) -> None:
        del stop_event
        if not self.scheduler.running:
            asyncio.get_running_loop().call_soon_threadsafe(self.scheduler.start)
        logger.info("workflow.start complete")

    async def stop(self) -> None:
        if self.scheduler.running:
            asyncio.get_running_loop().call_soon_threadsafe(self.scheduler.shutdown)
        logger.info("workflow.stop complete")
