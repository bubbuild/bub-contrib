from __future__ import annotations

import asyncio
from asyncio import Event

from apscheduler.schedulers.base import BaseScheduler
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.framework import BubFramework
from loguru import logger


class ScheduleChannel(Channel):
    name = "schedule"

    # Class-level runtime state (singleton per process)
    _queue: asyncio.Queue[ChannelMessage] | None = None
    _framework: BubFramework | None = None
    _worker_task: asyncio.Task[None] | None = None

    def __init__(self, scheduler: BaseScheduler, *, framework: BubFramework) -> None:
        self.scheduler = scheduler
        self._instance_framework = framework

    @classmethod
    async def enqueue_current(cls, payload: ChannelMessage) -> None:
        """Enqueue a payload to the current live schedule channel worker."""
        if cls._queue is None:
            raise RuntimeError(
                "no live schedule channel available, "
                f"cannot deliver message session_id={payload.session_id}"
            )
        await cls._queue.put(payload)

    async def start(self, stop_event: Event) -> None:
        ScheduleChannel._queue = asyncio.Queue()
        ScheduleChannel._framework = self._instance_framework
        ScheduleChannel._worker_task = asyncio.create_task(self._drain_queue())

        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self.scheduler.start)
        logger.info("schedule.start complete")

    async def stop(self) -> None:
        if ScheduleChannel._worker_task and not ScheduleChannel._worker_task.done():
            ScheduleChannel._worker_task.cancel()
            try:
                await ScheduleChannel._worker_task
            except asyncio.CancelledError:
                pass

        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(self.scheduler.shutdown)

        ScheduleChannel._queue = None
        ScheduleChannel._framework = None
        ScheduleChannel._worker_task = None
        logger.info("schedule.stop complete")

    @classmethod
    async def _drain_queue(cls) -> None:
        while cls._queue is not None:
            payload = await cls._queue.get()
            try:
                if cls._framework is not None:
                    await cls._framework.process_inbound(payload)
                else:
                    logger.error(
                        "schedule worker has no framework, dropping payload session_id={}",
                        payload.session_id,
                    )
            except Exception:
                logger.exception(
                    "schedule.process_inbound failed session_id={}",
                    payload.session_id,
                )
