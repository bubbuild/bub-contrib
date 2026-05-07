"""Tests for scheduled task delivery via live ScheduleChannel."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from bub.channels.message import ChannelMessage
from bub_schedule.channel import ScheduleChannel
from bub_schedule.jobs import run_scheduled_reminder


@pytest.fixture
def mock_framework():
    """Create a mock BubFramework."""
    framework = MagicMock()
    framework.process_inbound = AsyncMock()
    return framework


@pytest.fixture
def scheduler():
    """Create a background scheduler for testing."""
    sched = BackgroundScheduler()
    sched.start()
    yield sched
    try:
        sched.shutdown(wait=False)
    except Exception:
        pass


@pytest.fixture
def channel(scheduler, mock_framework):
    """Create a ScheduleChannel with mock framework."""
    return ScheduleChannel(scheduler, framework=mock_framework)


class TestEnqueueCurrent:
    """Test class-level enqueue_current and lifecycle."""

    async def test_raises_when_no_channel_started(self):
        """enqueue_current should raise RuntimeError before start()."""
        # Ensure clean state
        assert ScheduleChannel._queue is None
        payload = ChannelMessage(
            content="test", session_id="a:b", channel="a", chat_id="b"
        )
        with pytest.raises(RuntimeError, match="no live schedule channel"):
            await ScheduleChannel.enqueue_current(payload)

    async def test_succeeds_after_start(self, channel):
        """enqueue_current should succeed after start()."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            payload = ChannelMessage(
                content="hello", session_id="a:b", channel="a", chat_id="b"
            )
            await ScheduleChannel.enqueue_current(payload)  # should not raise
        finally:
            await channel.stop()

    async def test_cleared_after_stop(self, channel):
        """Class-level state should be None after stop()."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        await channel.stop()
        assert ScheduleChannel._queue is None
        assert ScheduleChannel._framework is None
        assert ScheduleChannel._worker_task is None


class TestRunScheduledReminder:
    """Test that run_scheduled_reminder uses ScheduleChannel.enqueue_current."""

    async def test_enqueues_payload(self, channel, mock_framework):
        """run_scheduled_reminder should enqueue via class method."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            await run_scheduled_reminder(
                message="test message",
                session_id="feishu:oc_123",
            )

            await asyncio.sleep(0.1)

            mock_framework.process_inbound.assert_called_once()
            payload = mock_framework.process_inbound.call_args[0][0]
            assert isinstance(payload, ChannelMessage)
            assert payload.content == "test message"
            assert payload.session_id == "feishu:oc_123"
            assert payload.channel == "feishu"
            assert payload.chat_id == "oc_123"
        finally:
            await channel.stop()

    async def test_fallback_session_channel(self, channel, mock_framework):
        """Session ID without ':' should default to schedule:default."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            await run_scheduled_reminder(
                message="reminder",
                session_id="simple_session",
            )

            await asyncio.sleep(0.1)

            mock_framework.process_inbound.assert_called_once()
            payload = mock_framework.process_inbound.call_args[0][0]
            assert payload.channel == "schedule"
            assert payload.chat_id == "default"
        finally:
            await channel.stop()

    async def test_no_channel_raises_error(self):
        """When no channel is started, should raise RuntimeError."""
        assert ScheduleChannel._queue is None
        with pytest.raises(RuntimeError, match="no live schedule channel"):
            await run_scheduled_reminder(
                message="should fail",
                session_id="feishu:oc_456",
            )


class TestWorkerProcessesPayloads:
    """Test that the worker correctly processes queued payloads."""

    async def test_worker_calls_process_inbound(self, channel, mock_framework):
        """Worker should dequeue and call framework.process_inbound."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            payload = ChannelMessage(
                content="hello",
                session_id="feishu:oc_789",
                channel="feishu",
                chat_id="oc_789",
            )
            await ScheduleChannel.enqueue_current(payload)
            await asyncio.sleep(0.1)

            mock_framework.process_inbound.assert_called_once_with(payload)
        finally:
            await channel.stop()

    async def test_worker_handles_multiple_payloads(self, channel, mock_framework):
        """Worker should process multiple payloads in order."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            for i in range(3):
                payload = ChannelMessage(
                    content=f"msg_{i}",
                    session_id=f"feishu:oc_{i}",
                    channel="feishu",
                    chat_id=f"oc_{i}",
                )
                await ScheduleChannel.enqueue_current(payload)

            await asyncio.sleep(0.3)
            assert mock_framework.process_inbound.call_count == 3
        finally:
            await channel.stop()

    async def test_worker_survives_process_inbound_error(self, channel, mock_framework):
        """Worker should not crash if process_inbound raises."""
        mock_framework.process_inbound.side_effect = [
            RuntimeError("boom"),
            None,
        ]

        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            payload1 = ChannelMessage(
                content="fail", session_id="a:b", channel="a", chat_id="b"
            )
            payload2 = ChannelMessage(
                content="ok", session_id="c:d", channel="c", chat_id="d"
            )
            await ScheduleChannel.enqueue_current(payload1)
            await ScheduleChannel.enqueue_current(payload2)

            await asyncio.sleep(0.3)
            assert mock_framework.process_inbound.call_count == 2
        finally:
            await channel.stop()
