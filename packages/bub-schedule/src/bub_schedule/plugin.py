import bub
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import BaseScheduler
from bub import hookimpl
from bub.channels import Channel
from bub.types import Envelope, MessageHandler, State

from bub_schedule.jobstore import JSONJobStore


def default_scheduler() -> BaseScheduler:
    job_file = bub.home / "jobs.json"
    job_store = JSONJobStore(job_file)
    return AsyncIOScheduler(jobstores={"default": job_store})


class ScheduleImpl:
    def __init__(self) -> None:
        from bub_schedule import tools  # noqa: F401

        self.scheduler = default_scheduler()

    @hookimpl
    def load_state(self, message: Envelope, session_id: str) -> State:
        return {"scheduler": self.scheduler}

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        from bub_schedule.channel import ScheduleChannel

        return [ScheduleChannel(self.scheduler)]


main = ScheduleImpl()
