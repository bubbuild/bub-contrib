import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from apscheduler.jobstores.base import ConflictingIdError, JobLookupError
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from bub import tool
from pydantic import BaseModel, Field
from republic import ToolContext

from bub_schedule.jobs import run_scheduled_reminder


def _ensure_scheduler(state: dict) -> BaseScheduler:
    if "scheduler" not in state:
        raise RuntimeError(
            "scheduler not found in state, is ScheduleImpl plugin loaded?"
        )
    return cast(BaseScheduler, state["scheduler"])


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


@tool(name="schedule.add", context=True, model=ScheduleAddInput)
def schedule_add(params: ScheduleAddInput, context: ToolContext) -> str:
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

    next_run = "-"
    if isinstance(job.next_run_time, datetime):
        next_run = job.next_run_time.isoformat()
    return f"scheduled: {job.id} next={next_run}"


@tool(name="schedule.remove", context=True)
def schedule_remove(job_id: str, context: ToolContext) -> str:
    """Remove one scheduled job by id."""
    scheduler = _ensure_scheduler(context.state)
    try:
        scheduler.remove_job(job_id)
    except JobLookupError as exc:
        raise RuntimeError(f"job not found: {job_id}") from exc
    return f"removed: {job_id}"


@tool(name="schedule.list", context=True)
def schedule_list(context: ToolContext) -> str:
    """List scheduled jobs for current workspace."""
    scheduler = _ensure_scheduler(context.state)
    jobs = scheduler.get_jobs()
    rows: list[str] = []
    for job in jobs:
        next_run = "-"
        if isinstance(job.next_run_time, datetime):
            next_run = job.next_run_time.isoformat()
        message = str(job.kwargs.get("message", ""))
        job_session = job.kwargs.get("session_id")
        if job_session and job_session != context.state.get("session_id", ""):
            continue
        rows.append(f"{job.id} next={next_run} msg={message}")

    if not rows:
        return "(no scheduled jobs)"

    return "\n".join(rows)


@tool(name="schedule.trigger", context=True)
def schedule_trigger(job_id: str, context: ToolContext) -> str:
    """Manually trigger a scheduled job to run immediately.
    
    Executes the job function directly without modifying the schedule.
    The next scheduled run remains unchanged.
    """
    scheduler = _ensure_scheduler(context.state)
    try:
        job = scheduler.get_job(job_id)
        if job is None:
            raise RuntimeError(f"job not found: {job_id}")
        
        # Execute job function directly, preserving original schedule
        job.func(*job.args, **job.kwargs)
        
        next_run = "-"
        if isinstance(job.next_run_time, datetime):
            next_run = job.next_run_time.isoformat()
        
        return f"triggered: {job_id} (next scheduled run: {next_run})"
    except JobLookupError as exc:
        raise RuntimeError(f"job not found: {job_id}") from exc