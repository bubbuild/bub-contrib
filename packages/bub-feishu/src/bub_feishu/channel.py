"""Feishu channel adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Generator

import lark_oapi as lark
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.types import MessageHandler
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class FeishuMention:
    open_id: str | None
    name: str | None
    key: str | None


@dataclass(frozen=True)
class FeishuReplyToMessage:
    message_id: str
    raw_content: str | None
    text: str | None
    sender_id: str | None


@dataclass(frozen=True)
class FeishuMessage:
    message_id: str
    chat_id: str
    chat_type: str
    message_type: str
    raw_content: str
    text: str
    mentions: tuple[FeishuMention, ...]
    parent_id: str | None
    root_id: str | None
    reply_to: FeishuReplyToMessage | None
    sender_id: str | None
    sender_open_id: str | None
    sender_union_id: str | None
    sender_user_id: str | None
    sender_name: str | None
    sender_type: str | None
    tenant_key: str | None
    create_time: str | None
    event_type: str | None
    raw_event: dict[str, Any]


class FeishuConfig(BaseSettings):
    """Feishu adapter config."""

    model_config = SettingsConfigDict(
        env_prefix="BUB_FEISHU_", env_file=".env", extra="ignore"
    )

    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    encrypt_key: str = ""
    allow_users: str | None = None
    allow_chats: str | None = None
    bot_open_id: str = ""
    log_level: str = "INFO"


def exclude_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _payload_timestamp(value: str | None) -> float | None:
    if not value:
        return None
    with contextlib.suppress(ValueError):
        if len(value) >= 13:
            return int(value) / 1000
        return float(value)
    return None


def _parse_collection(value: str | None) -> set[str]:
    if not value:
        return set()
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return {str(item).strip() for item in parsed if str(item).strip()}
    return {item.strip() for item in value.split(",") if item.strip()}


def _normalize_text(message_type: str, content: str) -> str:
    if not content:
        return ""
    parsed: dict[str, Any] | None = None
    with contextlib.suppress(json.JSONDecodeError):
        maybe_dict = json.loads(content)
        if isinstance(maybe_dict, dict):
            parsed = maybe_dict

    if message_type == "text":
        if parsed is not None:
            return str(parsed.get("text", "")).strip()
        return content.strip()
    if parsed is None:
        return f"[{message_type} message]"
    return f"[{message_type} message] {json.dumps(parsed, ensure_ascii=False)}"


class FeishuChannel(Channel):
    """Feishu adapter using Lark websocket subscription."""

    name = "feishu"

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._config = FeishuConfig()
        self._allow_users = _parse_collection(self._config.allow_users)
        self._allow_chats = _parse_collection(self._config.allow_chats)
        self._api_client: Any | None = None
        self._ws_client: Any | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_started = threading.Event()
        self._ws_stop_requested = threading.Event()
        self._main_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._pending_command_message_ids: dict[str, deque[str]] = {}
        self._sender_name_cache: dict[tuple[str, str], str | None] = {}

    @property
    def needs_debounce(self) -> bool:
        return True

    async def start(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event
        self._task = asyncio.create_task(self._main_loop_task())

    async def stop(self) -> None:
        await self._shutdown_ws()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def send(self, message: ChannelMessage) -> None:
        chat_id = message.chat_id or self._session_chat_id(message.session_id)
        if not chat_id:
            logger.warning(
                "feishu.outbound unresolved chat session_id={}", message.session_id
            )
            return

        reply_to_message_id = self._pop_command_message_id(message.session_id)
        await self._send_text(reply_to_message_id, chat_id, message.content)

    def _mentions_bot(self, message: FeishuMessage) -> bool:
        if self._config.bot_open_id and any(
            mention.open_id == self._config.bot_open_id for mention in message.mentions
        ):
            return True
        text = message.text.strip().lower()
        if "bub" in text:
            return True
        return any(
            "bub" in (mention.name or "").lower() for mention in message.mentions
        )

    async def _is_reply_to_bot(self, message: FeishuMessage) -> bool:
        """Check whether the parent message was sent by the bot."""
        if not message.reply_to:
            return False

        return message.reply_to.sender_id == self._config.bot_open_id

    async def is_mentioned(self, message: FeishuMessage) -> bool:
        text = message.text.strip()
        if text.startswith(","):
            return True
        if message.chat_type == "p2p":
            return True
        return self._mentions_bot(message) or await self._is_reply_to_bot(message)

    async def _build_message(self, message: FeishuMessage) -> ChannelMessage:
        session_id = f"{self.name}:{message.chat_id}"

        if message.text.strip().startswith(","):
            self._pending_command_message_ids.setdefault(session_id, deque()).append(
                message.message_id
            )
            return ChannelMessage(
                session_id=session_id,
                content=message.text.strip(),
                channel=self.name,
                chat_id=message.chat_id,
                kind="command",
                is_active=True,
            )

        payload = exclude_none(
            {
                "message": message.text,
                "message_id": message.message_id,
                "type": message.message_type,
                "sender_id": message.sender_id or "",
                "sender_name": message.sender_name,
                "sender_is_bot": message.sender_type == "bot",
                "reply_to_message": exclude_none(
                    {
                        "message_id": message.parent_id,
                        "text": message.reply_to.text if message.reply_to else None,
                        "sender_id": message.reply_to.sender_id
                        if message.reply_to
                        else None,
                    }
                )
                if message.parent_id
                else None,
            }
        )
        return ChannelMessage(
            session_id=session_id,
            content=json.dumps(payload, ensure_ascii=False),
            channel=self.name,
            chat_id=message.chat_id,
            is_active=await self.is_mentioned(message),
            output_channel="null",
        )

    async def _main_loop_task(self) -> None:
        if not self._config.app_id or not self._config.app_secret:
            raise RuntimeError("feishu app_id/app_secret is empty")
        if self._stop_event is None:
            raise RuntimeError("stop event is not initialized")

        self._main_loop = asyncio.get_running_loop()
        self._api_client = (
            lark.Client.builder()
            .app_id(self._config.app_id)
            .app_secret(self._config.app_secret)
            .log_level(
                getattr(
                    lark.LogLevel, self._config.log_level.upper(), lark.LogLevel.INFO
                )
            )
            .build()
        )

        event_handler = (
            lark.EventDispatcherHandler.builder(
                self._config.verification_token,
                self._config.encrypt_key,
            )
            .register_p2_im_message_receive_v1(self._on_message_event)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self._config.app_id,
            self._config.app_secret,
            event_handler=event_handler,
            log_level=getattr(
                lark.LogLevel, self._config.log_level.upper(), lark.LogLevel.INFO
            ),
        )

        logger.info(
            "feishu.start allow_users_count={} allow_chats_count={}",
            len(self._allow_users),
            len(self._allow_chats),
        )
        self._ws_stop_requested.clear()
        self._ws_started.clear()
        self._ws_thread = threading.Thread(
            target=self._run_ws_client, name="bub-feishu-ws", daemon=True
        )
        self._ws_thread.start()

        while not self._ws_started.is_set():
            await asyncio.sleep(0.05)

        try:
            await self._stop_event.wait()
        finally:
            await self._shutdown_ws()
            logger.info("feishu.stopped")

    def _on_message_event(self, data: Any) -> None:
        payload = self._to_payload_dict(data)
        normalized = self._normalize_event(payload)
        if normalized is None:
            return
        if not self._is_allowed(normalized):
            return
        if not normalized.text.strip():
            return
        if self._main_loop is None:
            logger.warning(
                "feishu.inbound no main loop for message {}", normalized.message_id
            )
            return
        future = asyncio.run_coroutine_threadsafe(
            self._dispatch_message(normalized), self._main_loop
        )
        with contextlib.suppress(Exception):
            future.result()

    async def _dispatch_message(self, message: FeishuMessage) -> None:
        message = await self._enrich_sender_name(message)
        message = await self._enrich_reply_to_message(message)
        payload = await self._build_message(message)
        await self._on_receive(payload)

    def _run_ws_client(self) -> None:
        if self._ws_client is None:
            return
        self._ws_started.set()
        try:
            self._ws_client.start()
        except RuntimeError:
            if not self._ws_stop_requested.is_set():
                logger.exception("feishu.ws.runtime_error")
        except Exception:
            if not self._ws_stop_requested.is_set():
                logger.exception("feishu.ws.error")

    async def _shutdown_ws(self) -> None:
        self._ws_stop_requested.set()
        client = self._ws_client
        if client is not None:
            for method_name in ("stop", "close"):
                method = getattr(client, method_name, None)
                if callable(method):
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(method)
                    break
            self._ws_client = None
        thread = self._ws_thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 1.0)
        self._ws_thread = None
        self._ws_started.clear()

    def _is_allowed(self, message: FeishuMessage) -> bool:
        if self._allow_chats and message.chat_id not in self._allow_chats:
            return False
        sender_tokens = {
            token
            for token in (
                message.sender_id,
                message.sender_open_id,
                message.sender_union_id,
                message.sender_user_id,
            )
            if token
        }
        return not (self._allow_users and sender_tokens.isdisjoint(self._allow_users))

    async def _send_text(
        self, reply_to_message_id: str | None, chat_id: str, text: str
    ) -> None:
        if self._api_client is None or not text.strip():
            return

        content = json.dumps({"text": text}, ensure_ascii=False)
        if reply_to_message_id:
            reply_request = (
                ReplyMessageRequest.builder()
                .message_id(reply_to_message_id)
                .request_body(
                    ReplyMessageRequestBody.builder()
                    .msg_type("text")
                    .content(content)
                    .reply_in_thread(False)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = await self._api_client.im.v1.message.areply(reply_request)
            if response.success():
                return

        create_request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(content)
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        response = await self._api_client.im.v1.message.acreate(create_request)
        if response.success():
            return
        logger.error(
            "feishu.create.failed code={} msg={} log_id={}",
            response.code,
            response.msg,
            response.get_log_id(),
        )

    async def _enrich_reply_to_message(self, message: FeishuMessage) -> FeishuMessage:
        if not message.parent_id or message.reply_to is not None:
            return message

        parent_message = await self._get_message_detail(message.parent_id)
        if not parent_message:
            return message

        return replace(
            message,
            reply_to=FeishuReplyToMessage(
                message_id=message.parent_id,
                raw_content=parent_message.get("raw_content"),
                text=parent_message.get("text"),
                sender_id=parent_message.get("sender_id"),
            ),
        )

    async def _enrich_sender_name(self, message: FeishuMessage) -> FeishuMessage:
        if message.sender_name:
            return message

        sender_name = await self._get_sender_name(message)
        if not sender_name:
            return message

        return replace(message, sender_name=sender_name)

    async def _get_sender_name(self, message: FeishuMessage) -> str | None:
        if self._api_client is None:
            return None

        for user_id_type, user_id in self._iter_sender_ids(message):
            cache_key = (user_id_type, user_id)
            if cache_key in self._sender_name_cache:
                return self._sender_name_cache[cache_key]

            sender_name = await self._fetch_user_name(user_id_type, user_id)
            self._sender_name_cache[cache_key] = sender_name
            if sender_name:
                return sender_name

        return None

    @staticmethod
    def _iter_sender_ids(
        message: FeishuMessage,
    ) -> Generator[tuple[str, str], None, None]:
        for user_id_type, user_id in (
            ("open_id", message.sender_open_id),
            ("user_id", message.sender_user_id),
            ("union_id", message.sender_union_id),
        ):
            if not user_id:
                continue
            yield (user_id_type, user_id)

    async def _fetch_user_name(self, user_id_type: str, user_id: str) -> str | None:
        if self._api_client is None or not user_id:
            return None

        from lark_oapi.api.contact.v3 import GetUserRequest

        request = (
            GetUserRequest.builder()
            .user_id_type(user_id_type)
            .department_id_type("open_department_id")
            .user_id(user_id)
            .build()
        )
        response = await self._api_client.contact.v3.user.aget(request)
        if (
            not response.success()
            or response.data is None
            or response.data.user is None
        ):
            logger.warning(
                "feishu.user.get.failed user_id_type={} user_id={} code={} msg={} log_id={}",
                user_id_type,
                user_id,
                response.code,
                response.msg,
                response.get_log_id(),
            )
            return None

        sender_name = getattr(response.data.user, "name", None)
        if not sender_name:
            return None
        return str(sender_name)

    async def _get_message_detail(self, message_id: str) -> dict[str, str | None]:
        if self._api_client is None or not message_id:
            return {}

        from lark_oapi.api.im.v1 import GetMessageRequest

        request = GetMessageRequest.builder().message_id(message_id).build()
        response = await self._api_client.im.v1.message.aget(request)
        if not response.success() or response.data is None:
            return {}

        data = response.data
        body = getattr(data, "body", None)
        raw_content = str(getattr(body, "content", "") or "")
        message_type = str(getattr(body, "message_type", "") or "unknown")
        sender = getattr(data, "sender", None)
        sender_id = getattr(sender, "id", None)

        return {
            "raw_content": raw_content,
            "text": _normalize_text(message_type, raw_content),
            "sender_id": sender_id,
        }

    @staticmethod
    def _session_chat_id(session_id: str) -> str:
        _, _, chat_id = session_id.partition(":")
        return chat_id

    def _pop_command_message_id(self, session_id: str) -> str | None:
        queue = self._pending_command_message_ids.get(session_id)
        if not queue:
            return None
        message_id = queue.popleft()
        if not queue:
            self._pending_command_message_ids.pop(session_id, None)
        return message_id

    @staticmethod
    def _to_payload_dict(data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        if lark is not None:
            with contextlib.suppress(Exception):
                raw = lark.JSON.marshal(data)
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
        value = getattr(data, "__dict__", None)
        if isinstance(value, dict):
            return value
        return {}

    @staticmethod
    def _normalize_event(payload: dict[str, Any]) -> FeishuMessage | None:
        event = payload.get("event")
        if not isinstance(event, dict):
            return None
        message = event.get("message")
        sender = event.get("sender")
        if not isinstance(message, dict) or not isinstance(sender, dict):
            return None

        sender_id = sender.get("sender_id")
        sender_id_obj = sender_id if isinstance(sender_id, dict) else {}
        mentions: list[FeishuMention] = []
        raw_mentions = message.get("mentions")
        if isinstance(raw_mentions, list):
            for raw in raw_mentions:
                if not isinstance(raw, dict):
                    continue
                mention_id = raw.get("id")
                mention_id_obj = mention_id if isinstance(mention_id, dict) else {}
                mentions.append(
                    FeishuMention(
                        open_id=mention_id_obj.get("open_id"),
                        name=raw.get("name"),
                        key=raw.get("key"),
                    )
                )

        message_type = str(message.get("message_type") or "unknown")
        raw_content = str(message.get("content") or "")
        normalized = FeishuMessage(
            message_id=str(message.get("message_id") or ""),
            chat_id=str(message.get("chat_id") or ""),
            chat_type=str(message.get("chat_type") or ""),
            message_type=message_type,
            raw_content=raw_content,
            text=_normalize_text(message_type, raw_content),
            mentions=tuple(mentions),
            parent_id=message.get("parent_id"),
            root_id=message.get("root_id"),
            reply_to=None,
            sender_id=sender_id_obj.get("open_id")
            or sender_id_obj.get("union_id")
            or sender_id_obj.get("user_id"),
            sender_open_id=sender_id_obj.get("open_id"),
            sender_union_id=sender_id_obj.get("union_id"),
            sender_user_id=sender_id_obj.get("user_id"),
            sender_name=sender.get("sender_name"),
            sender_type=sender.get("sender_type"),
            tenant_key=sender.get("tenant_key"),
            create_time=str(message.get("create_time") or ""),
            event_type=(payload.get("header") or {}).get("event_type"),
            raw_event=payload,
        )
        if not normalized.chat_id or not normalized.message_id:
            return None
        return normalized
