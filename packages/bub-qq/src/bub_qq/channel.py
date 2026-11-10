"""QQ channel with auth, OpenAPI and pluggable receive transports."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import bub
from bub.channels import Channel
from bub.channels.message import ChannelMessage
from bub.channels.contracts import MessageHandler
from loguru import logger

from .config import QQConfig
from .gateway.webhook import QQWebhookServer
from .gateway.websocket import QQWebSocketClient
from .inbound.c2c import QQC2CInboundService
from .inbound.group import GROUP_EVENTS
from .inbound.group import QQGroupInboundService
from .inbound.group import group_was_mentioned
from .inbound.images import image_media
from .protocol.models import QQAttachment
from .inbound.group import strip_mention_text
from .inbound.interaction import ACK_INTERACTION_TYPES
from .inbound.interaction import DEFAULT_REQUIRE_MENTION
from .inbound.interaction import INTERACTION_QUERY
from .inbound.interaction import INTERACTION_UPDATE
from .inbound.interaction import build_claw_cfg
from .inbound.interaction import build_interaction_channel_message
from .inbound.interaction import extract_claw_cfg_update
from .admins import AdminRegistry
from .approval import PendingApproval
from .approval import begin_approval_click
from .approval import attach_approval_outcomes
from .approval import complete_approval_click
from .approval import parse_approval_button
from .approval import send_notice
from .guard import Requester
from .guard import is_admin
from .inbound.interaction import parse_interaction_event
from .outbound.c2c import QQC2CSendService
from .outbound.group import QQGroupSendService
from .outbound.send_flow import C2C_PASSIVE_REPLIES_PER_MSG_ID
from .outbound.send_flow import C2C_PASSIVE_REPLY_WINDOW_SECONDS
from .outbound.send_flow import GROUP_PASSIVE_REPLIES_PER_MSG_ID
from .outbound.send_flow import GROUP_PASSIVE_REPLY_WINDOW_SECONDS
from .protocol.auth import QQTokenProvider
from .protocol.errors import QQOpenAPIError
from .protocol.openapi import QQOpenAPI
from .protocol.openapi import event_reply_id
from .runtime import set_active_channel
from .security import QQ_CONTEXT_KEY
from .security import QQAccessPolicy
from .session import QQInboundDeduper
from .session import QQSessionState
from .session import remember_session
from .store import QQPlatformStore
from .store import resolve_state_path
from .workspace import artifact_root

# Admin toggles for proactive messages, pushed when a group admin or C2C
# user flips the "allow active messages" switch in the QQ client.
_MSG_TOGGLE_EVENTS: dict[str, tuple[str, str, bool]] = {
    "GROUP_MSG_RECEIVE": ("group", "group_openid", True),
    "GROUP_MSG_REJECT": ("group", "group_openid", False),
    "C2C_MSG_RECEIVE": ("c2c", "openid", True),
    "C2C_MSG_REJECT": ("c2c", "openid", False),
}


ERROR_NOTICE = "处理这条消息时出错了，请稍后再试。"


class QQChannel(Channel):
    """QQ channel registration with reusable auth and OpenAPI client."""

    name = "qq"

    def __init__(self, on_receive: MessageHandler) -> None:
        self._on_receive = on_receive
        self._config = bub.ensure_config(QQConfig)
        self._token_provider = QQTokenProvider(self._config)
        self._openapi = QQOpenAPI(self._config, self._token_provider)
        self._webhook = QQWebhookServer(self._config, self._handle_transport_payload)
        self._websocket = QQWebSocketClient(
            self._config, self._openapi, self._handle_transport_payload
        )
        self._deduper = QQInboundDeduper(self._config.inbound_dedupe_size)
        self._session_state = QQSessionState(
            max_entries=self._config.session_state_size
        )
        self._policy = QQAccessPolicy.from_config(self._config)
        self._platform_store = QQPlatformStore(resolve_state_path(self._config))
        self._admins = AdminRegistry(self._platform_store, self._config)
        self._workspace = Path.cwd().resolve()
        # In tool reply mode the model must reply through the qq.send tool,
        # so direct model output is routed to the "null" channel and dropped.
        suppress_direct_output = self._config.reply_mode == "tool"
        self._c2c_inbound = QQC2CInboundService(
            channel_name=self.name,
            deduper=self._deduper,
            state=self._session_state,
            policy=self._policy,
            suppress_direct_output=suppress_direct_output,
            is_admin=self._is_admin,
        )
        self._group_inbound = QQGroupInboundService(
            channel_name=self.name,
            deduper=self._deduper,
            state=self._session_state,
            policy=self._policy,
            suppress_direct_output=suppress_direct_output,
            is_admin=self._is_admin,
            wake_on=self._config.group_wake,
        )
        self._c2c_send = QQC2CSendService(
            channel_name=self.name,
            receive_mode=self._config.receive_mode,
            state=self._session_state,
            openapi=self._openapi,
            passive_reply_window_seconds=_override(
                self._config.passive_reply_window_seconds,
                C2C_PASSIVE_REPLY_WINDOW_SECONDS,
            ),
            passive_replies_per_msg_id=_override(
                self._config.passive_replies_per_msg_id, C2C_PASSIVE_REPLIES_PER_MSG_ID
            ),
            workspace=self._workspace,
        )
        self._group_send = QQGroupSendService(
            channel_name=self.name,
            receive_mode=self._config.receive_mode,
            state=self._session_state,
            openapi=self._openapi,
            passive_reply_window_seconds=_override(
                self._config.passive_reply_window_seconds,
                GROUP_PASSIVE_REPLY_WINDOW_SECONDS,
            ),
            passive_replies_per_msg_id=_override(
                self._config.passive_replies_per_msg_id,
                GROUP_PASSIVE_REPLIES_PER_MSG_ID,
            ),
            active_messages=self._config.active_messages,
            platform_store=self._platform_store,
            workspace=self._workspace,
        )
        set_active_channel(self)

    @property
    def needs_debounce(self) -> bool:
        return True

    async def start(self, stop_event: asyncio.Event | None) -> None:
        if not self._config.appid or not self._config.secret:
            raise RuntimeError("qq appid/secret is empty")
        self._check_security_config()
        self._admins.bootstrap_code()

        mode = self._normalize_receive_mode()
        if mode == "webhook":
            await self._webhook.start()
            logger.info(
                "qq.start mode=webhook reply_mode={} workspace={} artifacts={} token_url={} openapi_base_url={} webhook=http://{}:{}{} websocket=disabled",
                self._config.reply_mode,
                self._workspace,
                artifact_root(self._workspace),
                self._config.token_url,
                self._config.openapi_base_url,
                self._config.webhook_host,
                self._config.webhook_port,
                self._config.webhook_path,
            )
            return

        await self._websocket.start(stop_event)
        logger.info(
            "qq.start mode=websocket reply_mode={} workspace={} artifacts={} token_url={} openapi_base_url={} intents={} webhook=disabled",
            self._config.reply_mode,
            self._workspace,
            artifact_root(self._workspace),
            self._config.token_url,
            self._config.openapi_base_url,
            self._config.websocket_intents,
        )

    async def stop(self) -> None:
        await self._webhook.stop()
        await self._websocket.stop()
        await self._openapi.aclose()
        logger.info("qq.stopped")

    async def send(self, message: ChannelMessage) -> None:
        await self.send_for_result(message)

    async def send_for_result(self, message: ChannelMessage) -> dict[str, object] | None:
        """Send and return the send-service result (used by the qq.send tool)."""

        if message.kind == "error":
            # Bub reports failures as "An error occurred at stage ...: <exc>";
            # the exception text can carry URLs, paths or keys. Log it, and
            # tell the chat only that something went wrong.
            logger.error(
                "qq.outbound.error session_id={} detail={}",
                message.session_id,
                message.content,
            )
            message = replace(message, content=ERROR_NOTICE, kind="normal")
        if _is_group_target(self.name, message):
            return await self._group_send.send(message)
        return await self._c2c_send.send(message)

    async def _handle_transport_payload(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        event_type = payload.get("t")
        # The event exactly as QQ sent it (verbose: 1), for protocol debugging.
        # It holds message text, openids and attachment URLs with download keys.
        logger.opt(lazy=True).debug(
            "qq.transport.payload op={} t={} payload={}",
            lambda: op,
            lambda: event_type,
            lambda: json.dumps(payload, ensure_ascii=False, default=str),
        )
        if op != 0:
            logger.info("qq.transport.ignored op={} t={}", op, event_type)
            return
        if event_type == "READY":
            logger.info("qq.websocket.ready")
            return
        if event_type == "RESUMED":
            logger.info("qq.websocket.resumed")
            return
        if event_type == "C2C_MESSAGE_CREATE":
            await self._handle_c2c_message(payload)
            return
        if event_type in GROUP_EVENTS:
            await self._handle_group_message(payload)
            return
        if event_type == "INTERACTION_CREATE":
            await self._handle_interaction(payload)
            return
        if event_type in _MSG_TOGGLE_EVENTS:
            self._handle_msg_toggle(event_type, payload)
            return
        logger.info("qq.transport.unhandled event={} op={}", event_type, op)

    def _handle_msg_toggle(self, event_type: str, payload: dict[str, Any]) -> None:
        scope, id_field, allowed = _MSG_TOGGLE_EVENTS[event_type]
        data = payload.get("d")
        openid = str(data.get(id_field) or "").strip() if isinstance(data, dict) else ""
        if not openid:
            logger.warning(
                "qq.msg_toggle.invalid_payload event={} reason=missing_{}",
                event_type,
                id_field,
            )
            return
        self._platform_store.update(scope, openid, active_messages=allowed)
        logger.info(
            "qq.msg_toggle event={} scope={} openid={} active_messages={}",
            event_type,
            scope,
            openid,
            allowed,
        )

    async def _handle_c2c_message(self, payload: dict[str, Any]) -> None:
        parsed = self._c2c_inbound.parse_inbound(payload)
        if parsed is None:
            return
        message, channel_message = parsed
        logger.info(
            "qq.c2c.inbound session_id={} user_openid={} content_len={} attachments={}",
            channel_message.session_id,
            message.user_openid,
            len(message.content),
            len(message.attachments),
        )
        requester = Requester(scope="c2c", sender_id=message.user_openid)
        if await self._handle_admin_command(
            message.content, requester, channel_message
        ):
            return
        channel_message = self._attach_images(
            message.inbound_attachments, channel_message, direct=True
        )
        await self._on_receive(attach_approval_outcomes(channel_message))

    async def _handle_group_message(self, payload: dict[str, Any]) -> None:
        parsed = self._group_inbound.parse_inbound(payload)
        if parsed is None:
            return
        message, channel_message = parsed
        logger.info(
            "qq.group.inbound session_id={} group_openid={} member_openid={} was_mentioned={} is_active={} content_len={}",
            channel_message.session_id,
            message.group_openid,
            message.member_openid,
            group_was_mentioned(message),
            channel_message.is_active,
            len(message.content),
        )
        requester = Requester(
            scope="group",
            sender_id=message.member_openid,
            group_openid=message.group_openid,
        )
        if await self._handle_admin_command(
            strip_mention_text(message.content, message.mentions),
            requester,
            channel_message,
        ):
            return
        channel_message = self._attach_images(
            message.inbound_attachments, channel_message, direct=group_was_mentioned(message)
        )
        await self._on_receive(attach_approval_outcomes(channel_message))

    def _attach_images(
        self,
        attachments: tuple[QQAttachment, ...],
        channel_message: ChannelMessage,
        *,
        direct: bool,
    ) -> ChannelMessage:
        """Attach a message's images for the model to see, per inbound_images.

        ``attachments`` are the message's own and its quoted messages', in
        index order. ``direct`` marks messages addressed to the bot (private
        chats, @ in a group). Attached images are flagged in the payload so
        the model knows they are in the message; the others it can open with
        qq.view_image.
        """

        mode = self._config.inbound_images
        if (
            not attachments
            or channel_message.kind == "command"
            or mode == "off"
            or (mode == "direct" and not direct)
        ):
            return channel_message
        try:
            payload = json.loads(channel_message.content)
        except ValueError:
            return channel_message
        if not isinstance(payload, dict):
            return channel_message
        media, indices = image_media(
            attachments,
            workspace=self._workspace,
            message_id=str(payload.get("message_id") or ""),
            max_count=self._config.inbound_images_per_message,
            max_bytes=self._config.inbound_image_max_bytes,
        )
        if not media:
            return channel_message
        listed = _listed_attachments(payload)
        for index in indices:
            if index in listed:
                listed[index]["attached"] = True
        return replace(
            channel_message,
            content=json.dumps(payload, ensure_ascii=False),
            media=[*channel_message.media, *media],
        )

    @property
    def session_state(self) -> QQSessionState:
        return self._session_state

    def _is_admin(self, requester: Requester) -> bool:
        return is_admin(self._config, requester)

    async def dispatch_approved_command(self, pending: PendingApproval) -> None:
        """Re-submit an approved comma command; the Guard consumes its token when it runs."""

        qq_context = {
            "scope": pending.requester.scope,
            "sender_id": pending.requester.sender_id,
            "sender_name": pending.requester_name,
            "session_id": pending.session_id,
            # save_state records the command's output under this id.
            "approval_id": pending.id,
            "approval_preview": pending.preview,
        }
        if pending.requester.group_openid:
            qq_context["group_openid"] = pending.requester.group_openid
        message = ChannelMessage(
            session_id=pending.session_id,
            content=pending.command_line,
            channel=self.name,
            chat_id=pending.chat_id,
            kind="command",
            is_active=True,
            context={QQ_CONTEXT_KEY: qq_context},
        )
        await self._on_receive(message)

    async def _handle_interaction(self, payload: dict[str, Any]) -> None:
        event = parse_interaction_event(payload)
        if event is None:
            return
        event_type = event["type"]
        if event_type in {INTERACTION_QUERY, INTERACTION_UPDATE}:
            group_openid = event["group_openid"]
            if event_type == INTERACTION_UPDATE and group_openid:
                update = extract_claw_cfg_update(event)
                if update:
                    self._platform_store.update("group", group_openid, **update)
                    logger.info(
                        "qq.interaction.claw_cfg_updated group_openid={} update={}",
                        group_openid,
                        update,
                    )
            require_mention = (
                self._platform_store.require_mention(group_openid)
                if group_openid
                else None
            )
            claw_cfg = build_claw_cfg(
                require_mention=require_mention or DEFAULT_REQUIRE_MENTION
            )
            try:
                await self._openapi.put_interaction(
                    interaction_id=event["id"],
                    code=0,
                    data={"claw_cfg": claw_cfg},
                )
            except QQOpenAPIError as exc:
                logger.warning(
                    "qq.interaction.ack_failed id={} code={} error={}",
                    event["id"],
                    exc.error_code,
                    exc.error_message,
                )
            return
        if event_type in ACK_INTERACTION_TYPES:
            resolved = event.get("resolved") if isinstance(event.get("resolved"), dict) else {}
            button_data = str(resolved.get("button_data") or "")
            parsed = parse_approval_button(button_data)
            if parsed is not None:
                approval_id, decision = parsed
                group_openid = str(event.get("group_openid") or "")
                if group_openid:
                    operator = Requester(
                        scope="group",
                        sender_id=str(event.get("group_member_openid") or ""),
                        group_openid=group_openid,
                    )
                else:
                    operator = Requester(
                        scope="c2c", sender_id=str(event.get("user_openid") or "")
                    )
                plan = begin_approval_click(
                    approval_id=approval_id,
                    decision=decision,
                    operator=operator,
                    config=self._config,
                )
                try:
                    await self._openapi.put_interaction(
                        interaction_id=event["id"],
                        code=plan.ack_code,
                    )
                except QQOpenAPIError as exc:
                    logger.warning(
                        "qq.interaction.ack_failed id={} type={} code={} error={}",
                        event["id"],
                        event_type,
                        exc.error_code,
                        exc.error_message,
                    )
                try:
                    notice = await complete_approval_click(plan)
                except Exception as exc:
                    logger.warning(
                        "qq.approval.complete_failed id={} error={}",
                        approval_id,
                        exc,
                    )
                    notice = f"审批执行失败: {exc}"
                if notice:
                    group_openid = str(event.get("group_openid") or "")
                    chat_id = (
                        f"group:{group_openid}"
                        if group_openid
                        else f"c2c:{event.get('user_openid') or ''}"
                    )
                    session_id = f"{self.name}:{chat_id}"
                    await send_notice(session_id, chat_id, notice)
                return
            try:
                await self._openapi.put_interaction(
                    interaction_id=event["id"],
                    code=0,
                )
            except QQOpenAPIError as exc:
                logger.warning(
                    "qq.interaction.ack_failed id={} type={} code={} error={}",
                    event["id"],
                    event_type,
                    exc.error_code,
                    exc.error_message,
                )
            channel_message = build_interaction_channel_message(
                self.name,
                event,
                suppress_direct_output=self._config.reply_mode == "tool",
            )
            if channel_message is None:
                logger.info(
                    "qq.interaction.unhandled type={} reason=unsupported_scene",
                    event_type,
                )
                return
            remember_session(
                self._session_state,
                session_id=channel_message.session_id,
                message_id=event_reply_id(str(event["id"])),
                timestamp=str(event.get("timestamp") or "") or None,
            )
            qq_context = channel_message.context.get(QQ_CONTEXT_KEY)
            logger.info(
                "qq.interaction.inbound session_id={} type={} sender_id={}",
                channel_message.session_id,
                event_type,
                qq_context.get("sender_id") if isinstance(qq_context, dict) else "",
            )
            await self._on_receive(channel_message)
            return
        logger.info("qq.interaction.unhandled type={}", event_type)

    def _check_security_config(self) -> None:
        config = self._config
        for removed, replacement in (
            ("exec_approval", "group_shell"),
            ("workspace_jail", "group_shell / shell_sandbox"),
        ):
            if getattr(config, removed, None) is not None:
                logger.warning(
                    "qq.config.removed option={} is ignored since 0.3.0; use {}",
                    removed,
                    replacement,
                )
        if config.c2c_access == "allow_users" and not config.allow_users.strip():
            raise RuntimeError(
                "qq c2c_access=allow_users needs a non-empty allow_users; an empty"
                " list would open tools to every private-chat user"
            )
        if config.group_shell == "approval" and config.shell_sandbox == "none":
            logger.warning(
                "qq.security.shell_unsandboxed commands approved by admins run"
                " directly on this host; run bub in a sandbox and set"
                " shell_sandbox=external, or set group_shell=deny"
            )
        if config.shell_sandbox == "external":
            logger.info(
                "qq.security.shell_sandbox assuming bash runs in an external"
                " sandbox; the plugin does not isolate commands"
            )

    async def _handle_admin_command(
        self, text: str, requester: Requester, channel_message: ChannelMessage
    ) -> bool:
        """Answer ,qq.claim / ,qq.admins here; True when handled."""

        reply = self._admins.handle(text, requester)
        if reply is None:
            return False
        if reply:
            await send_notice(channel_message.session_id, channel_message.chat_id, reply)
        await self._send_admin_notices()
        return True

    async def _send_admin_notices(self) -> None:
        """Tell admins privately about registrations made with their codes."""

        while self._admins.notices:
            openid, text = self._admins.notices.pop(0)
            try:
                await send_notice(f"{self.name}:c2c:{openid}", f"c2c:{openid}", text)
            except Exception as exc:
                # Proactive C2C messages need the user's opt-in on QQ; the
                # registration is still in the log (qq.admin.registered).
                logger.warning("qq.admin.notice_failed openid={} error={}", openid, exc)

    def _normalize_receive_mode(self) -> str:
        mode = (self._config.receive_mode or "").strip().lower()
        if mode not in {"webhook", "websocket"}:
            raise RuntimeError(
                f"qq receive_mode must be webhook or websocket, got {self._config.receive_mode!r}"
            )
        return mode


def _override[T](value: T | None, default: T) -> T:
    return default if value is None else value


def _is_group_target(channel_name: str, message: ChannelMessage) -> bool:
    chat_id = message.chat_id or ""
    session_id = message.session_id or ""
    return chat_id.startswith("group:") or session_id.startswith(
        f"{channel_name}:group:"
    )


def _listed_attachments(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """The payload's attachment entries by index.

    Own attachments are numbered by position, quoted ones by their ``index``.
    """

    entries: dict[int, dict[str, Any]] = {}
    listed = payload.get("attachments")
    if isinstance(listed, list):
        entries.update(
            (position, entry) for position, entry in enumerate(listed) if isinstance(entry, dict)
        )

    def walk(quoted: object) -> None:
        for item in quoted if isinstance(quoted, list) else ():
            if not isinstance(item, dict):
                continue
            for entry in item.get("attachments") or ():
                if isinstance(entry, dict) and isinstance(entry.get("index"), int):
                    entries[entry["index"]] = entry
            walk(item.get("messages"))

    walk(payload.get("quoted_messages"))
    return entries
