from __future__ import annotations

from bub.channels.message import ChannelMessage

SCHEDULE_SUBPROCESS_TIMEOUT_SECONDS = 300


async def run_scheduled_reminder(
    message: str, session_id: str, workspace: str | None = None
) -> None:
    from bub_schedule.channel import ScheduleChannel

    if ":" in session_id:
        channel, chat_id = session_id.split(":", 1)
    else:
        channel = "schedule"
        chat_id = "default"

    payload = ChannelMessage(
        content=message,
        session_id=session_id,
        channel=channel,
        chat_id=chat_id,
    )
    await ScheduleChannel.enqueue_current(payload)
