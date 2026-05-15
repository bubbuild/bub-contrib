from __future__ import annotations

import asyncio
from asyncio import Event

from apscheduler.schedulers.base import BaseScheduler
from bub.channels import Channel
from bub.framework import BubFramework
from loguru import logger


class ScheduleChannel(Channel):
    name = "schedule"

    # Class-level runtime state (singleton per process)
    _framework: BubFramework | None = None

    def __init__(self, scheduler: BaseScheduler, *, framework: BubFramework) -> None:
        self.scheduler = scheduler
        self._instance_framework = framework

    @classmethod
    def current_framework(cls) -> BubFramework:
        """Return the live framework bound to the current gateway process."""
        if cls._framework is None:
            raise RuntimeError(
                "no live schedule framework available, cannot deliver scheduled message"
            )
        return cls._framework

    async def start(self, stop_event: Event) -> None:
        ScheduleChannel._framework = self._instance_framework

        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self.scheduler.start)
        logger.info("schedule.start complete")

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self.scheduler.shutdown)

        ScheduleChannel._framework = None
        logger.info("schedule.stop complete")
