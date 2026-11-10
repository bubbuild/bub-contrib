from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Hashable
from dataclasses import dataclass

from bub.envelope import Envelope
from bub.turn import TurnState


@dataclass(frozen=True, slots=True, eq=False)
class SteeringReceipt:
    """Tracks whether one queued steering message reached a model step."""

    key: Hashable
    message: Envelope
    delivered: asyncio.Future[None]


class ACPSteeringInbox:
    """Session-scoped steering inbox with atomic delivery receipts.

    Bub's built-in agent drains this object at model-step boundaries. Receipts
    let the ACP adapter distinguish a message consumed by the active turn from
    one that arrived after its final drain and must start a new turn.
    """

    def __init__(self) -> None:
        self._messages: defaultdict[Hashable, deque[SteeringReceipt]] = defaultdict(
            deque
        )

    # Queue mutations contain no awaits, so they are atomic on the event loop.

    async def enqueue_message(self, message: Envelope, state: TurnState) -> None:
        await self.enqueue_with_receipt(message, state)

    async def enqueue_with_receipt(
        self, message: Envelope, state: TurnState
    ) -> SteeringReceipt:
        key = self._key(state)
        receipt = SteeringReceipt(
            key, message, asyncio.get_running_loop().create_future()
        )
        self._messages[key].append(receipt)
        return receipt

    async def drain_messages(self, state: TurnState) -> list[Envelope]:
        key = self._key(state)
        queued = self._messages.pop(key, ())
        for item in queued:
            if not item.delivered.done():
                item.delivered.set_result(None)
        return [item.message for item in queued]

    async def claim_pending(self, receipt: SteeringReceipt) -> Envelope | None:
        """Remove and return a receipt's message if no model step consumed it."""

        queued = self._messages.get(receipt.key)
        if queued is None:
            return None
        try:
            queued.remove(receipt)
        except ValueError:
            return None
        if not queued:
            self._messages.pop(receipt.key, None)
        return receipt.message

    def message_count(self, state: TurnState) -> int:
        return len(self._messages.get(self._key(state), ()))

    @staticmethod
    def _key(state: TurnState) -> Hashable:
        thread_id = state.get("_runtime_thread_id")
        if isinstance(thread_id, Hashable) and thread_id:
            return thread_id
        session_id = state.get("session_id")
        if isinstance(session_id, Hashable) and session_id:
            return session_id
        return "default"
