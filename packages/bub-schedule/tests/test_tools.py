"""Tests for schedule.add / schedule.list / schedule.remove structured results."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from bub.tools import ToolContext

from bub_schedule import tools


@pytest.fixture
def scheduler():
    sched = BackgroundScheduler()
    sched.start(paused=True)
    yield sched
    sched.shutdown(wait=False)


def _context(scheduler, session_id: str = "test_session") -> ToolContext:
    return ToolContext(
        tape=None, state={"scheduler": scheduler, "session_id": session_id}
    )


def _run(tool, *args: Any, **kwargs: Any) -> Any:
    return asyncio.run(tool.run(*args, **kwargs))


def test_schedule_tools_declare_output_schema():
    for tool in (
        tools.schedule_add,
        tools.schedule_remove,
        tools.schedule_list,
        tools.schedule_trigger,
    ):
        assert tool.output_schema is not None


def test_schedule_add_list_remove_roundtrip(scheduler):
    context = _context(scheduler)

    added = _run(
        tools.schedule_add, after_seconds=3600, message="stand up", context=context
    )
    assert added["message"] == "stand up"
    assert added["next_run"] is not None
    assert tools.schedule_add.render(added) == (
        f"scheduled: {added['job_id']} next={added['next_run']}"
    )

    listed = _run(tools.schedule_list, context=context)
    assert listed == {"jobs": [added]}
    assert tools.schedule_list.render(listed) == (
        f"{added['job_id']} next={added['next_run']} msg=stand up"
    )

    removed = _run(tools.schedule_remove, added["job_id"], context=context)
    assert removed == {"job_id": added["job_id"]}
    assert tools.schedule_remove.render(removed) == f"removed: {added['job_id']}"

    empty = _run(tools.schedule_list, context=context)
    assert empty == {"jobs": []}
    assert tools.schedule_list.render(empty) == "(no scheduled jobs)"


def test_schedule_list_filters_other_sessions(scheduler):
    _run(
        tools.schedule_add,
        interval_seconds=60,
        message="mine",
        context=_context(scheduler, "a"),
    )
    _run(
        tools.schedule_add,
        interval_seconds=60,
        message="theirs",
        context=_context(scheduler, "b"),
    )

    listed = _run(tools.schedule_list, context=_context(scheduler, "a"))

    assert [job["message"] for job in listed["jobs"]] == ["mine"]


def test_schedule_remove_unknown_job_raises(scheduler):
    with pytest.raises(RuntimeError, match="job not found: missing"):
        _run(tools.schedule_remove, "missing", context=_context(scheduler))
