"""Slack Socket Mode channel adapter for Bub.

Socket Mode lets the bot receive events over a WebSocket (the ``xapp-`` token)
without exposing a public HTTP endpoint, while replies are posted through the
Web API (the ``xoxb-`` bot token). Uses only the official ``slack_sdk`` package.

Activation rules:

* The channel is only ``enabled`` when **both** tokens are present, so ``bub
  gateway`` silently skips it on a machine that only does CLI/``bub run``.
* In channels (public/private), the bot reacts only when @-mentioned. In DMs it
  reacts to every message.
* Messages from bots (including itself) and message subtypes (joins, edits,
  deletes, file shares, ...) are ignored to avoid echo loops and noise.

Replies are delivered through Bub's normal outbound router: the framework
renders the model's text into a :class:`ChannelMessage` targeted at this channel
and calls :meth:`send`; we leave ``output_channel`` at its default so routing
falls back to ``slack``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

import bub
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from loguru import logger
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from bub_slack.config import SlackSettings

#: Slack rejects ``text`` longer than 4000 chars per ``chat.postMessage``.
_SLACK_CHUNK_SIZE = 3900

#: Standard (workspace-built-in) emoji for the inbound ack lifecycle — no custom
#: emoji setup required. Added on accept, swapped to done on the first reply.
_ACK_EMOJI_IN_PROGRESS = "hourglass"
_ACK_EMOJI_DONE = "white_check_mark"

#: session_id -> (channel_id, inbound_ts) for messages we've reacted :hourglass:
#: to on accept. Consumed (and swapped to :white_check_mark:) by the first
#: outbound ``send()`` for that session — one-shot via ``pop``. In-memory only
#: (resets on restart, like ``_active_threads``). A turn that errors with no
#: reply leaves the :hourglass: in place — non-fatal and intentionally accepted
#: per the "ack on receive" design.
_ACK_PENDING: dict[str, tuple[str, str]] = {}


class SlackChannel(Channel):
    """A Bub channel backed by Slack Socket Mode."""

    name = "slack"

    def __init__(
        self,
        on_receive: MessageHandler,
        *,
        settings: SlackSettings | None = None,
    ) -> None:
        self._on_receive = on_receive
        self._settings = settings or bub.ensure_config(SlackSettings)
        self._bot_token = self._settings.bot_token
        self._app_token = self._settings.app_token
        self._allow_channels = {
            c.strip() for c in (self._settings.allow_channels or "").split(",") if c.strip()
        }
        self._allow_users = {
            u.strip() for u in (self._settings.allow_users or "").split(",") if u.strip()
        }

        self._web_client: AsyncWebClient | None = None
        self._client: SocketModeClient | None = None
        self._bot_user_id: str | None = None
        # Roots of threads the bot has posted into. Once the bot replies in a
        # thread, subsequent replies in that thread count as "addressed" without
        # a fresh @mention (mirrors Telegram's reply-to-bot addressing). In-memory
        # only — resets on restart, so the first message in any thread still needs
        # to be addressed explicitly.
        self._active_threads: set[str] = set()
        # Optional readiness marker (env BUB_HEALTH_FILE). Touched once the
        # Socket Mode session is up so a k8s startup/readiness probe can tell a
        # healthy gateway from one still (or no longer) connected to Slack.
        self._health_file = os.environ.get("BUB_HEALTH_FILE", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self._bot_token) and bool(self._app_token)

    @property
    def needs_debounce(self) -> bool:
        # Merge rapid-fire bursts (e.g. a user hitting Enter per line) into one
        # turn, exactly like the Telegram channel.
        return True

    async def start(self, stop_event: asyncio.Event) -> None:
        self._web_client = AsyncWebClient(token=self._bot_token)
        auth = await self._web_client.auth_test()
        self._bot_user_id = auth.get("user_id")
        logger.info(
            "slack.start connected bot={} team={} url={} user_id={}",
            auth.get("user"),
            auth.get("team"),
            auth.get("url"),
            self._bot_user_id,
        )

        # ``SocketModeClient`` must be constructed inside a running loop (it
        # creates an aiohttp session). ``connect()`` establishes the WSS session
        # and spawns its own background receive/monitor tasks (held on the
        # client instance, so they are not GC'd), then returns.
        #
        # IMPORTANT: do NOT ``await stop_event.wait()`` here. The ChannelManager
        # calls every channel's ``start()`` sequentially and only then runs the
        # message-consumer loop. Blocking here would enqueue Slack events that
        # never get processed. start() must kick off the listener and return,
        # mirroring the Telegram channel; shutdown is handled by ``stop()``.
        self._client = SocketModeClient(app_token=self._app_token, web_client=self._web_client)
        self._client.socket_mode_request_listeners.append(self._on_slack_event)
        await self._client.connect()
        logger.info(
            "slack.start socket_mode listening allow_channels={} allow_users={}",
            len(self._allow_channels),
            len(self._allow_users),
        )
        self._touch_health()

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()
        self._clear_health()
        logger.info("slack.stopped")

    # ------------------------------------------------------------------
    # Readiness marker (consumed by the k8s startup/readiness probe)
    # ------------------------------------------------------------------

    def _touch_health(self) -> None:
        """Write the readiness marker once Socket Mode is connected."""
        if not self._health_file:
            return
        try:
            Path(self._health_file).parent.mkdir(parents=True, exist_ok=True)
            Path(self._health_file).write_text("ready\n", encoding="utf-8")
        except OSError:
            logger.opt(exception=True).warning(
                "slack.health could not write marker {}", self._health_file
            )

    def _clear_health(self) -> None:
        """Remove the readiness marker on shutdown."""
        if not self._health_file:
            return
        with contextlib.suppress(OSError):
            Path(self._health_file).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Inbound ack reactions (best-effort; never raise into the bot flow)
    # ------------------------------------------------------------------

    async def _react(self, channel_id: str, ts: str, name: str) -> None:
        """Add an emoji reaction. Best-effort: any failure (missing
        ``reactions:write`` scope, network, already-reacted) is logged at debug
        and swallowed so message IO is never disrupted."""
        if self._web_client is None:
            return
        try:
            await self._web_client.reactions_add(channel=channel_id, timestamp=ts, name=name)
        except Exception:  # noqa: BLE001 — reactions must never block the bot
            logger.opt(exception=True).debug("slack.reactions add {} failed", name)

    async def _unreact(self, channel_id: str, ts: str, name: str) -> None:
        """Remove an emoji reaction. Same best-effort contract as :meth:`_react`."""
        if self._web_client is None:
            return
        try:
            await self._web_client.reactions_remove(channel=channel_id, timestamp=ts, name=name)
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).debug("slack.reactions remove {} failed", name)

    async def _on_slack_event(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledge the envelope immediately so Slack does not retry.
        with contextlib.suppress(Exception):
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        event = (req.payload or {}).get("event") or {}
        try:
            if event.get("type") == "message":
                await self._handle_message(event)
        except Exception:  # noqa: BLE001 — never let a listener error kill the socket
            logger.opt(exception=True).warning("slack.event_handler failed")

    async def _handle_message(self, event: dict[str, Any]) -> None:
        # Ignore message subtypes (channel_join, message_changed, file_share, ...).
        if event.get("subtype"):
            return
        # Ignore anything posted by a bot (covers other bots and our own echo).
        if event.get("bot_id") or event.get("bot_profile"):
            return

        user_id = event.get("user") or ""
        if self._bot_user_id and user_id == self._bot_user_id:
            return

        channel_id = event.get("channel") or ""
        text = (event.get("text") or "").strip()
        channel_type = event.get("channel_type") or ""
        ts = event.get("ts") or ""
        thread_ts = event.get("thread_ts") or ""

        mention = f"<@{self._bot_user_id}>" if self._bot_user_id else ""
        mentioned = bool(mention) and mention in text
        # Accept the message when any of:
        #   1. it is a DM (always addressed),
        #   2. it explicitly @mentions the bot,
        #   3. it is a reply inside a thread the bot is already participating in
        #      (so users do not have to re-mention on every turn of a help thread).
        in_active_thread = bool(thread_ts) and thread_ts in self._active_threads
        addressed = channel_type == "im" or mentioned or in_active_thread
        if not addressed:
            return

        # The channel allow-list only constrains shared channels — DMs are
        # always private to the user, so they're never filtered by it (the user
        # allow-list still applies). Otherwise setting BUB_SLACK_ALLOW_CHANNELS
        # to lock the bot to specific channels would silently drop every DM.
        if channel_type != "im" and self._allow_channels and channel_id not in self._allow_channels:
            return
        if self._allow_users and user_id not in self._allow_users:
            return

        if self._bot_user_id:
            clean = text.replace(mention, "").strip()
        else:
            clean = text
        if not clean:
            return

        session_id = _session_id(channel_id, channel_type, thread_ts, ts)
        message = ChannelMessage(
            session_id=session_id,
            channel=self.name,
            chat_id=channel_id,
            content=clean,
            context={
                "sender_id": user_id,
                "channel_type": channel_type,
                "ts": ts,
                "thread_ts": thread_ts,
                "root_ts": thread_ts,
                "links": _extract_links(text),
            },
            is_active=True,
        )
        # Ack the inbound message right away (:hourglass:) and remember it so the
        # first reply (send()) can swap it to :white_check_mark:. Best-effort — a
        # reaction API failure is logged and never blocks message handling.
        _ACK_PENDING[session_id] = (channel_id, ts)
        await self._react(channel_id, ts, _ACK_EMOJI_IN_PROGRESS)

        await self._on_receive(message)

    async def send(self, message: ChannelMessage) -> None:
        if self._web_client is None:
            return
        channel_id = message.chat_id
        text = _extract_text(message.content)
        if not text.strip():
            return

        # Prefer the inbound-captured thread_ts when present, but Bub's builtin
        # ``render_outbound`` rebuilds the outbound ChannelMessage *without*
        # copying inbound context, so thread_ts is usually gone by the time we
        # get here. Recover it from the thread-scoped session_id
        # (``slack:{channel}:{thread_root}``) instead — that always reflects the
        # thread the inbound turn belongs to.
        thread_ts = _thread_ts_from_context(message) or _thread_ts_from_session(message.session_id)
        if thread_ts:
            self._active_threads.add(thread_ts)

        for chunk in _chunk_text(text, _SLACK_CHUNK_SIZE):
            await self._web_client.chat_postMessage(
                channel=channel_id,
                text=chunk,
                mrkdwn=True,
                unfurl_links=False,
                **({"thread_ts": thread_ts} if thread_ts else {}),
            )

        # First reply for this session: swap the inbound ack :hourglass: →
        # :white_check_mark:. One-shot (pop) so multi-chunk / multi-message
        # replies react exactly once. Best-effort, never blocks.
        ack = _ACK_PENDING.pop(message.session_id, None)
        if ack is not None:
            ack_channel, ack_ts = ack
            await self._unreact(ack_channel, ack_ts, _ACK_EMOJI_IN_PROGRESS)
            await self._react(ack_channel, ack_ts, _ACK_EMOJI_DONE)


def _extract_text(content: str) -> str:
    """The outbound ``content`` is plain model text; tolerate JSON wrappers."""

    if not content:
        return ""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content
    if isinstance(data, dict):
        return str(data.get("message") or data.get("text") or data.get("content") or "")
    return content


def _chunk_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    return [text[i : i + size] for i in range(0, len(text), size)]


def _session_id(channel_id: str, channel_type: str, thread_ts: str, ts: str) -> str:
    """Build a thread-aware session id.

    DMs stay channel-scoped (``slack:{channel_id}``) so a private 1:1 conversation
    keeps continuous memory. Shared channels are thread-scoped
    (``slack:{channel_id}:{thread_root}``), where ``thread_root`` is the thread's
    parent ts for a reply, or the message ts for a top-level post — so two threads
    in the same channel (and two top-level mentions) never share short-term state
    or get merged by the debounce/coalesce layer.

    Slack channel ids contain no colons, so splitting this id on ``:`` is safe.
    """
    if channel_type == "im":
        return f"slack:{channel_id}"
    thread_root = thread_ts or ts
    if not thread_root:
        return f"slack:{channel_id}"
    return f"slack:{channel_id}:{thread_root}"


def _thread_ts_from_context(message: ChannelMessage) -> str:
    """Read a thread_ts recorded on the inbound context, if any survived routing."""
    ctx = getattr(message, "context", {}) or {}
    if isinstance(ctx, dict):
        return ctx.get("thread_ts") or ""
    return ""


def _thread_ts_from_session(session_id: str) -> str:
    """Recover a thread ts from a thread-scoped session id.

    Thread-scoped ids look like ``slack:{channel}:{ts}`` (3+ colon-separated
    parts); DM and bare ids have only 2 parts and yield ``""``.
    """
    if not session_id:
        return ""
    parts = session_id.split(":")
    if len(parts) >= 3:
        return parts[2]
    return ""


# Bare http(s) URLs and Slack ``<http://…|label>`` link wrappers.
_LINK_RE = re.compile(r"<(?P<url>https?://[^|>]+)(?:\|[^>]*)?>|(?P<bare>https?://\S+)")


def _extract_links(text: str) -> list[str]:
    """Pull URLs out of a Slack message body (handles ``<url|label>`` wrappers)."""
    links: list[str] = []
    for match in _LINK_RE.finditer(text or ""):
        links.append(match.group("url") or match.group("bare") or "")
    return [link for link in links if link]
