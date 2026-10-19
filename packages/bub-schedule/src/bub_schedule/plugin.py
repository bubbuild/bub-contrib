import bub
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import BaseScheduler
from bub import hookimpl
from bub.channels import Channel
from bub.framework import BubFramework
from bub.channels.contracts import MessageHandler
from bub.core.tools import Tool
from bub.envelope import Envelope
from bub.turn import TurnState

from bub_schedule.jobstore import JSONJobStore


def default_scheduler() -> BaseScheduler:
    job_file = bub.home / "jobs.json"
    job_store = JSONJobStore(job_file)
    return AsyncIOScheduler(jobstores={"default": job_store})


class ScheduleImpl:
    def __init__(self, framework: BubFramework | None = None) -> None:
        self.framework = framework
        self.scheduler = default_scheduler()

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> TurnState:
        return {"scheduler": self.scheduler}

    @hookimpl
    def provide_tools(self) -> list[Tool]:
        from bub_schedule.tools import TOOLS

        return list(TOOLS)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        from bub_schedule.channel import ScheduleChannel

        return [ScheduleChannel(self.scheduler, framework=self.framework)]
