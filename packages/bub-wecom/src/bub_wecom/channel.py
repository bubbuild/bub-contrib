from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from loguru import logger
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WeComSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BUB_WECOM_", env_file=".env", extra="ignore"
    )

    bot_id: str = Field(default="", description="WeCom AI bot id")
    secret: str = Field(default="", description="WeCom AI bot secret")
    websocket_url: str = Field(
        default="wss://openws.work.weixin.qq.com",
        description="WeCom websocket URL",
    )
    dm_policy: str = Field(default="open", description="DM policy: open|disabled|allowlist")
    allow_from: str | None = Field(
        default=None,
        description="Comma separated DM user allowlist",
    )
    group_policy: str = Field(
        default="open", description="Group policy: open|disabled|allowlist"
    )
    group_allow_from: str | None = Field(
        default=None,
        description="Comma separated group allowlist",
    )
    @property
    def enabled(self) -> bool:
        return bool(self.bot_id.strip() and self.secret.strip())


def _parse_collection(value: str | None) -> set[str]:
    if not value:
        return set()
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return {str(item).strip().lower() for item in parsed if str(item).strip()}
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _normalize_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    for prefix in ("wecom:", "user:", "group:"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
    return token


def _is_allowed(policy: str, allowlist: set[str], target: str) -> bool:
    normalized_policy = policy.strip().lower()
    normalized_target = _normalize_token(target)
    if normalized_policy == "disabled":
        return False
    if normalized_policy == "allowlist":
        return "*" in allowlist or normalized_target in allowlist
    return True


def _extract_text(body: dict[str, Any], msg_type: str) -> str:
    if msg_type == "text":
        text_block = body.get("text")
        if isinstance(text_block, dict):
            return str(text_block.get("content") or "").strip()
        return str(body.get("content") or "").strip()
    if msg_type == "mixed":
        mixed = body.get("mixed")
        items = mixed.get("msg_item") if isinstance(mixed, dict) else None
        if isinstance(items, list):
            return "\n".join(
                str(item.get("text", {}).get("content") or "").strip()
                for item in items
                if isinstance(item, dict) and str(item.get("msgtype") or "").lower() == "text"
            ).strip()
    return ""


def _extract_quote(body: dict[str, Any]) -> dict[str, Any] | None:
    quote = body.get("quote")
    if not isinstance(quote, dict):
        return None
    msg_type = str(quote.get("msgtype") or "").strip().lower()
    result: dict[str, Any] = {"msgtype": msg_type} if msg_type else {}
    text_block = quote.get("text")
    if isinstance(text_block, dict):
        text = str(text_block.get("content") or "").strip()
        if text:
            result["text"] = {"content": text}
    return result or None


def _frame_req_id(frame: dict[str, Any]) -> str:
    headers = frame.get("headers")
    if isinstance(headers, dict):
        return str(headers.get("req_id") or "").strip()
    return ""


def _frame_body(frame: dict[str, Any]) -> dict[str, Any]:
    body = frame.get("body")
    return body if isinstance(body, dict) else {}


def _frame_chat_id(frame: dict[str, Any]) -> str:
    body = _frame_body(frame)
    sender = body.get("from")
    sender_block = sender if isinstance(sender, dict) else {}
    sender_id = str(sender_block.get("userid") or sender_block.get("id") or "").strip()
    return str(body.get("chatid") or sender_id).strip()


def _frame_type(frame: dict[str, Any]) -> str:
    body = _frame_body(frame)
    msg_type = str(body.get("msgtype") or "").strip().lower()
    if msg_type:
        return f"message.{msg_type}"
    event_type = str(body.get("eventtype") or body.get("event_type") or "").strip().lower()
    if event_type:
        return f"event.{event_type}"
    return "message"


def frame_to_message(frame: dict[str, Any]) -> ChannelMessage | None:
    body = _frame_body(frame)
    sender = body.get("from")
    sender_block = sender if isinstance(sender, dict) else {}
    sender_id = str(sender_block.get("userid") or sender_block.get("id") or "").strip()
    chat_id = str(body.get("chatid") or sender_id).strip()
    if not chat_id:
        return None

    chat_type = "group" if str(body.get("chattype") or "").strip().lower() == "group" else "dm"
    msg_type = str(body.get("msgtype") or "text").strip().lower()
    message_id = str(body.get("msgid") or _frame_req_id(frame)).strip()
    text = _extract_text(body, msg_type)
    session_id = f"wecom:{chat_id}"

    is_command = text.startswith(",")

    if is_command:
        return ChannelMessage(
            session_id=session_id,
            channel="wecom",
            chat_id=chat_id,
            content=text,
            kind="command",
            is_active=True,
            context={
                "sender_id": sender_id,
                "chat_type": chat_type,
                "message_id": message_id,
                "message_type": msg_type,
            },
        )

    payload = {
        "message": text,
        "message_id": message_id,
        "message_type": msg_type,
        "sender_id": sender_id,
        "chat_type": chat_type,
    }
    quote = _extract_quote(body)
    if quote is not None:
        payload["quote"] = quote
    return ChannelMessage(
        session_id=session_id,
        channel="wecom",
        chat_id=chat_id,
        content=json.dumps(payload, ensure_ascii=False),
        is_active=False,
        lifespan=None,
        context={
            "sender_id": sender_id,
            "chat_type": chat_type,
            "message_id": message_id,
            "message_type": msg_type,
        },
    )


@dataclass
class _WeComStreamSession:
    frame: dict[str, Any]
    stream_id: str


class _SdkClient(Protocol):
    def on(self, event: str, handler: Callable[..., Any] | None = None) -> Any: ...

    async def connect(self) -> Any: ...

    def disconnect(self) -> Any: ...

    async def send_message(self, chatid: str, body: dict[str, Any]) -> Any: ...

    async def reply_stream(self, frame: dict[str, Any], stream_id: str, content: str, finish: bool = False) -> Any: ...


def _build_sdk_client(settings: WeComSettings) -> _SdkClient:
    from aibot import WSClient, WSClientOptions

    return cast(
        _SdkClient,
        WSClient(
            WSClientOptions(
                bot_id=settings.bot_id,
                secret=settings.secret,
                ws_url=settings.websocket_url,
            )
        ),
    )


class WeComChannel(Channel):
    name = "wecom"

    def __init__(
        self,
        on_receive: MessageHandler,
        client_factory: Callable[[WeComSettings], _SdkClient] | None = None,
    ) -> None:
        self._on_receive = on_receive
        self._settings = WeComSettings()
        self._allow_users = _parse_collection(self._settings.allow_from)
        self._allow_groups = _parse_collection(self._settings.group_allow_from)
        self._client_factory = client_factory or _build_sdk_client
        self._client: _SdkClient | None = None
        self._started = False
        self._stream_sessions: dict[str, _WeComStreamSession] = {}

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @property
    def needs_debounce(self) -> bool:
        return True

    async def start(self, stop_event: asyncio.Event) -> None:
        del stop_event
        if not self.enabled:
            logger.info("wecom.start skipped because channel is disabled")
            return
        if self._started:
            return
        self._client = self._client_factory(self._settings)
        self._register_handlers()
        await self._client.connect()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        if self._client is None:
            self._started = False
            return
        result = self._client.disconnect()
        if inspect.isawaitable(result):
            await cast(Awaitable[Any], result)
        self._client = None
        self._started = False

    async def send(self, message: ChannelMessage) -> None:
        if self._client is None:
            raise RuntimeError("wecom client is not initialized")
        stream_session = self._stream_sessions.pop(message.session_id, None)
        if stream_session is not None and hasattr(self._client, "reply_stream"):
            await self._client.reply_stream(
                stream_session.frame,
                stream_session.stream_id,
                message.content[:4000],
                True,
            )
            return
        chat_id = message.chat_id or self._session_chat_id(message.session_id)
        if not chat_id:
            logger.warning("wecom.outbound missing chat_id session_id={}", message.session_id)
            return
        await self._client.send_message(
            chat_id,
            {
                "msgtype": "markdown",
                "markdown": {"content": message.content[:4000]},
            },
        )

    def _register_handlers(self) -> None:
        if self._client is None:
            return
        for event in ("message.text", "message.image", "message.mixed", "message.voice", "message.file"):
            self._register_handler(event, self._handle_frame)

    def _register_handler(self, event: str, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if self._client is None:
            return
        registration = getattr(self._client, "on")
        try:
            registration(event, handler)
        except TypeError:
            decorator = registration(event)
            decorator(handler)

    async def _handle_frame(self, frame: dict[str, Any]) -> None:
        normalized = frame_to_message(frame)
        if normalized is None:
            return
        if not self._message_allowed(normalized):
            return
        normalized.is_active = self._is_active(normalized)
        normalized.lifespan = self._message_lifespan(normalized.session_id, frame)
        await self._on_receive(normalized)

    def _message_allowed(self, message: ChannelMessage) -> bool:
        sender_id = str(message.context.get("sender_id") or "")
        chat_type = str(message.context.get("chat_type") or "dm")
        if chat_type == "group":
            return _is_allowed(self._settings.group_policy, self._allow_groups, message.chat_id)
        return _is_allowed(self._settings.dm_policy, self._allow_users, sender_id)

    def _is_active(self, message: ChannelMessage) -> bool:
        if message.kind == "command":
            return True
        return True

    @contextlib.asynccontextmanager
    async def _message_lifespan(self, session_id: str, frame: dict[str, Any]):
        stream_started = False
        try:
            if self._client is not None and hasattr(self._client, "reply_stream"):
                chat_id = _frame_chat_id(frame)
                if chat_id:
                    stream_session = _WeComStreamSession(
                        frame=frame,
                        stream_id=f"stream-{uuid.uuid4().hex}",
                    )
                    self._stream_sessions[session_id] = stream_session
                    await self._client.reply_stream(
                        stream_session.frame,
                        stream_session.stream_id,
                        "Thinking...",
                        False,
                    )
                    stream_started = True
            yield
        finally:
            if not stream_started:
                self._stream_sessions.pop(session_id, None)

    @staticmethod
    def _session_chat_id(session_id: str) -> str:
        prefix = "wecom:"
        if session_id.startswith(prefix):
            return session_id[len(prefix) :]
        return ""
