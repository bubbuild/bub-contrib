"""Tests for scheduled task delivery through the live framework."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    """Create a mock scheduler; job execution itself is not under test here."""
    return MagicMock()


@pytest.fixture
def channel(scheduler, mock_framework):
    """Create a ScheduleChannel with mock framework."""
    return ScheduleChannel(scheduler, framework=mock_framework)


class TestCurrentFramework:
    """Test class-level live framework registration lifecycle."""

    async def test_raises_when_no_channel_started(self):
        """current_framework should raise RuntimeError before start()."""
        ScheduleChannel._framework = None
        with pytest.raises(RuntimeError, match="no live schedule framework"):
            ScheduleChannel.current_framework()

    async def test_returns_framework_after_start(self, channel, mock_framework):
        """start() should register the live framework."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            assert ScheduleChannel.current_framework() is mock_framework
        finally:
            await channel.stop()

    async def test_cleared_after_stop(self, channel):
        """Class-level state should be None after stop()."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        await channel.stop()
        assert ScheduleChannel._framework is None


class TestRunScheduledReminder:
    """Test that run_scheduled_reminder directly uses the live framework."""

    async def test_processes_payload_via_live_framework(self, channel, mock_framework):
        """run_scheduled_reminder should call framework.process_inbound directly."""
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            await run_scheduled_reminder(
                message="test message",
                session_id="feishu:oc_123",
            )

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

            mock_framework.process_inbound.assert_called_once()
            payload = mock_framework.process_inbound.call_args[0][0]
            assert payload.channel == "schedule"
            assert payload.chat_id == "default"
        finally:
            await channel.stop()

    async def test_no_framework_raises_error(self):
        """When no live framework is registered, should raise RuntimeError."""
        ScheduleChannel._framework = None
        with pytest.raises(RuntimeError, match="no live schedule framework"):
            await run_scheduled_reminder(
                message="should fail",
                session_id="feishu:oc_456",
            )

    async def test_propagates_process_inbound_error(self, channel, mock_framework):
        """Delivery should surface framework.process_inbound failures directly."""
        mock_framework.process_inbound.side_effect = RuntimeError("boom")
        stop_event = asyncio.Event()
        await channel.start(stop_event)
        try:
            with pytest.raises(RuntimeError, match="boom"):
                await run_scheduled_reminder(
                    message="fail",
                    session_id="feishu:oc_999",
                )
        finally:
            await channel.stop()
