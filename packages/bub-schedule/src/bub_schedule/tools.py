import uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast, final

from apscheduler.job import Job
from apscheduler.jobstores.base import ConflictingIdError, JobLookupError
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bub import tool
from bub.tools import ToolContext
from pydantic import BaseModel, Field

from bub_schedule.jobs import run_scheduled_reminder


def _ensure_scheduler(state: dict) -> BaseScheduler:
    if "scheduler" not in state:
        raise RuntimeError(
            "scheduler not found in state, is ScheduleImpl plugin loaded?"
        )
    return cast(BaseScheduler, state["scheduler"])


@final
class ScheduledJob(TypedDict):
    job_id: str
    next_run: str | None
    message: str


@final
class ScheduleRemoveResult(TypedDict):
    job_id: str


@final
class ScheduleListResult(TypedDict):
    jobs: list[ScheduledJob]


def _next_run(job: Job) -> str | None:
    if isinstance(job.next_run_time, datetime):
        return job.next_run_time.isoformat()
    return None


def _scheduled_job(job: Job) -> ScheduledJob:
    return {
        "job_id": job.id,
        "next_run": _next_run(job),
        "message": str(job.kwargs.get("message", "")),
    }


def _render_list(result: ScheduleListResult) -> str:
    rows = [
        f"{job['job_id']} next={job['next_run'] or '-'} msg={job['message']}"
        for job in result["jobs"]
    ]
    return "\n".join(rows) or "(no scheduled jobs)"


class ScheduleAddInput(BaseModel):
    after_seconds: int | None = Field(
        None, description="If set, schedule to run after this many seconds from now"
    )
    interval_seconds: int | None = Field(
        None, description="If set, repeat at this interval"
    )
    cron: str | None = Field(
        None,
        description="If set, run with cron expression in crontab format: minute hour day month day_of_week",
    )
    message: str = Field(
        ...,
        description="Reminder message to send, prefix the message with ',' to run a bash command instead",
    )


@tool(
    name="schedule.add",
    context=True,
    model=ScheduleAddInput,
    renderer=lambda result: (
        f"scheduled: {result['job_id']} next={result['next_run'] or '-'}"
    ),
)
def schedule_add(params: ScheduleAddInput, context: ToolContext) -> ScheduledJob:
    """Schedule a reminder message to be sent to current session in the future."""
    job_id = str(uuid.uuid4())[:8]
    if params.after_seconds is not None:
        trigger = DateTrigger(
            run_date=datetime.now(UTC) + timedelta(seconds=params.after_seconds)
        )
    elif params.interval_seconds is not None:
        trigger = IntervalTrigger(seconds=params.interval_seconds)
    else:
        try:
            trigger = CronTrigger.from_crontab(params.cron)
        except ValueError as exc:
            raise RuntimeError(f"invalid cron expression: {params.cron}") from exc
    scheduler = _ensure_scheduler(context.state)
    workspace = context.state.get("_runtime_workspace")
    try:
        job = scheduler.add_job(
            run_scheduled_reminder,
            trigger=trigger,
            id=job_id,
            kwargs={
                "message": params.message,
                "session_id": context.state.get("session_id", ""),
                "workspace": str(workspace) if workspace else None,
            },
            coalesce=True,
            max_instances=1,
        )
    except ConflictingIdError as exc:
        raise RuntimeError(f"job id already exists: {job_id}") from exc
    return _scheduled_job(job)


@tool(
    name="schedule.remove",
    context=True,
    renderer=lambda result: f"removed: {result['job_id']}",
)
def schedule_remove(job_id: str, context: ToolContext) -> ScheduleRemoveResult:
    """Remove one scheduled job by id."""
    scheduler = _ensure_scheduler(context.state)
    try:
        scheduler.remove_job(job_id)
    except JobLookupError as exc:
        raise RuntimeError(f"job not found: {job_id}") from exc
    return {"job_id": job_id}


@tool(name="schedule.list", context=True, renderer=_render_list)
def schedule_list(context: ToolContext) -> ScheduleListResult:
    """List scheduled jobs for current workspace."""
    scheduler = _ensure_scheduler(context.state)
    session_id = context.state.get("session_id", "")
    jobs: list[ScheduledJob] = []
    for job in scheduler.get_jobs():
        job_session = job.kwargs.get("session_id")
        if job_session and job_session != session_id:
            continue
        jobs.append(_scheduled_job(job))
    return {"jobs": jobs}


@tool(
    name="schedule.trigger",
    context=True,
    renderer=lambda result: (
        f"triggered: {result['job_id']}"
        f" (next scheduled run: {result['next_run'] or '-'})"
    ),
)
async def schedule_trigger(job_id: str, context: ToolContext) -> ScheduledJob:
    """Manually trigger a scheduled job to run immediately.

    Executes the job function directly without modifying the schedule.
    The next scheduled run remains unchanged.
    """
    import inspect

    scheduler = _ensure_scheduler(context.state)
    try:
        job = scheduler.get_job(job_id)
        if job is None:
            raise RuntimeError(f"job not found: {job_id}")

        # Execute job function directly, preserving original schedule
        result = job.func(*job.args, **job.kwargs)

        # Handle async job functions
        if inspect.iscoroutine(result):
            await result

        return _scheduled_job(job)
    except JobLookupError as exc:
        raise RuntimeError(f"job not found: {job_id}") from exc
