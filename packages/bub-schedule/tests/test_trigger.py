"""Tests for schedule.trigger tool."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from republic import ToolContext

from bub_schedule import tools


def _trigger(job_id: str, context: ToolContext) -> str:
    """Helper to call the schedule_trigger tool."""
    import asyncio
    result = tools.schedule_trigger.handler(job_id, context=context)
    # If it's a coroutine, run it
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


@pytest.fixture
def scheduler():
    """Create a background scheduler for testing."""
    sched = BackgroundScheduler()
    sched.start()
    yield sched
    sched.shutdown(wait=False)


@pytest.fixture
def context(scheduler):
    """Create a ToolContext with scheduler state."""
    return ToolContext(
        tape=None,
        run_id="test_run",
        state={
            "scheduler": scheduler,
            "session_id": "test_session",
        },
    )


def test_schedule_trigger_executes_job_immediately(scheduler, context):
    """Test that schedule.trigger executes the job function immediately."""
    # Track execution
    execution_log: list[dict[str, Any]] = []
    
    def test_job(value: str):
        execution_log.append({
            "value": value,
            "timestamp": datetime.now(UTC),
        })
    
    # Schedule job to run in 1 hour (should not run naturally during test)
    job = scheduler.add_job(
        test_job,
        trigger=IntervalTrigger(minutes=5),
        id="test_job_1",
        kwargs={"value": "test_value"},
        next_run_time=datetime.now(UTC) + timedelta(hours=1),
    )
    
    original_next_run = job.next_run_time
    
    # Verify job hasn't executed yet
    assert len(execution_log) == 0
    
    # Trigger manually
    result = _trigger("test_job_1", context)
    
    # Verify execution happened
    assert len(execution_log) == 1
    assert execution_log[0]["value"] == "test_value"
    
    # Verify schedule is preserved
    job = scheduler.get_job("test_job_1")
    assert job is not None
    assert job.next_run_time == original_next_run
    
    # Verify result message
    assert "triggered: test_job_1" in result
    assert "next scheduled run:" in result


def test_schedule_trigger_preserves_interval_baseline(scheduler, context):
    """Test that triggering doesn't shift the interval trigger baseline."""
    execution_times: list[datetime] = []
    
    def test_job():
        execution_times.append(datetime.now(UTC))
    
    # Create interval job: run every 5 minutes, first run in 10 seconds
    initial_run_time = datetime.now(UTC) + timedelta(seconds=10)
    job = scheduler.add_job(
        test_job,
        trigger=IntervalTrigger(minutes=5),
        id="test_job_2",
        next_run_time=initial_run_time,
    )
    
    original_next_run = job.next_run_time
    
    # Manually trigger now
    _trigger("test_job_2", context)
    
    # Verify manual execution
    assert len(execution_times) == 1
    
    # Verify next_run_time didn't change
    job = scheduler.get_job("test_job_2")
    assert job is not None
    assert job.next_run_time == original_next_run
    
    # The baseline should still be the original time, not shifted by manual trigger
    expected_next_after_original = original_next_run + timedelta(minutes=5)
    
    # Wait for scheduled run
    time.sleep(11)  # Wait past the original scheduled time
    
    # Should have executed at scheduled time
    assert len(execution_times) == 2
    
    # Next run should be 5 minutes after original, not after manual trigger
    job = scheduler.get_job("test_job_2")
    if job and job.next_run_time:
        # Allow 1 second tolerance for execution time
        time_diff = abs((job.next_run_time - expected_next_after_original).total_seconds())
        assert time_diff < 1, f"Next run shifted from original baseline: {job.next_run_time} vs expected {expected_next_after_original}"


def test_schedule_trigger_with_args_and_kwargs(scheduler, context):
    """Test that trigger correctly passes args and kwargs to job function."""
    result_log: list[dict] = []
    
    def job_with_params(arg1: str, arg2: int, kwarg1: str = "default"):
        result_log.append({
            "arg1": arg1,
            "arg2": arg2,
            "kwarg1": kwarg1,
        })
    
    scheduler.add_job(
        job_with_params,
        trigger=IntervalTrigger(minutes=5),
        id="test_job_3",
        args=["value1", 42],
        kwargs={"kwarg1": "custom"},
        next_run_time=datetime.now(UTC) + timedelta(hours=1),
    )
    
    # Trigger
    _trigger("test_job_3", context)
    
    # Verify parameters passed correctly
    assert len(result_log) == 1
    assert result_log[0]["arg1"] == "value1"
    assert result_log[0]["arg2"] == 42
    assert result_log[0]["kwarg1"] == "custom"


def test_schedule_trigger_job_not_found(scheduler, context):
    """Test that triggering non-existent job raises error."""
    with pytest.raises(RuntimeError, match="job not found: nonexistent_job"):
        _trigger("nonexistent_job", context)


def test_schedule_trigger_no_scheduler_in_state():
    """Test that missing scheduler in state raises error."""
    context = ToolContext(tape=None, run_id="test_run", state={})
    
    with pytest.raises(RuntimeError, match="scheduler not found in state"):
        _trigger("any_job", context)


def test_schedule_trigger_returns_next_run_info(scheduler, context):
    """Test that trigger returns information about next scheduled run."""
    def dummy_job():
        pass
    
    next_run = datetime.now(UTC) + timedelta(minutes=30)
    scheduler.add_job(
        dummy_job,
        trigger=IntervalTrigger(minutes=5),
        id="test_job_4",
        next_run_time=next_run,
    )
    
    result = _trigger("test_job_4", context)
    
    # Result should contain job ID and next run time
    assert "test_job_4" in result
    assert "next scheduled run:" in result
    # Should contain ISO format timestamp
    assert next_run.strftime("%Y-%m-%d") in result
