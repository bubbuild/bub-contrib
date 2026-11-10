from __future__ import annotations

import json
from typing import Any

from bub.channels.message import ChannelMessage
from loguru import logger

from ..protocol.openapi import event_reply_id
from ..security import QQ_CONTEXT_KEY
from .common import exclude_none

INTERACTION_QUERY = 2001
INTERACTION_UPDATE = 2002
INTERACTION_BUTTON = 11
INTERACTION_MENU = 12
ACK_INTERACTION_TYPES = frozenset({INTERACTION_BUTTON, INTERACTION_MENU})

REQUIRE_MENTION_VALUES = {"always", "mention"}
DEFAULT_REQUIRE_MENTION = "always"


def parse_interaction_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("d")
    if not isinstance(data, dict):
        logger.warning("qq.interaction.invalid_payload reason=missing_d")
        return None
    interaction_id = str(data.get("id") or "").strip()
    if not interaction_id:
        logger.warning("qq.interaction.invalid_payload reason=missing_id")
        return None
    inner = data.get("data")
    inner_type = inner.get("type") if isinstance(inner, dict) else None
    resolved = inner.get("resolved") if isinstance(inner, dict) else None
    event_type = inner_type if inner_type is not None else data.get("type")
    return {
        "id": interaction_id,
        "type": event_type,
        "scene": str(data.get("scene") or "").strip(),
        "user_openid": str(data.get("user_openid") or "").strip(),
        "group_openid": str(data.get("group_openid") or "").strip(),
        "group_member_openid": str(data.get("group_member_openid") or "").strip(),
        "timestamp": str(data.get("timestamp") or "").strip(),
        "resolved": resolved if isinstance(resolved, dict) else {},
    }


def build_interaction_channel_message(
    channel_name: str,
    event: dict[str, Any],
    *,
    suppress_direct_output: bool = False,
) -> ChannelMessage | None:
    """Adapt a button/menu click into a ChannelMessage.

    PUT ack must happen before this; this only maps the click into the
    session so a later plugin-owned flow (e.g. admin approval) can inspect
    sender identity without waiting on the model.
    """

    group_openid = str(event.get("group_openid") or "").strip()
    user_openid = str(event.get("user_openid") or "").strip()
    scene = str(event.get("scene") or "").strip()
    if scene == "guild":
        return None
    if group_openid or scene == "group":
        if not group_openid:
            return None
        session_id = f"{channel_name}:group:{group_openid}"
        chat_id = f"group:{group_openid}"
        sender_id = str(event.get("group_member_openid") or "").strip()
        scope = "group"
        chat_type = "group"
    elif user_openid or scene == "c2c":
        if not user_openid:
            return None
        session_id = f"{channel_name}:c2c:{user_openid}"
        chat_id = f"c2c:{user_openid}"
        sender_id = user_openid
        scope = "c2c"
        chat_type = "c2c"
    else:
        return None

    resolved = event.get("resolved")
    if not isinstance(resolved, dict):
        resolved = {}
    button_id = str(resolved.get("button_id") or "").strip() or None
    button_data = str(resolved.get("button_data") or "").strip() or None
    payload = exclude_none(
        {
            "message": button_data or button_id or "",
            "message_id": event["id"],
            "type": "interaction",
            "interaction_type": event.get("type"),
            "button_id": button_id,
            "button_data": button_data,
            "sender_id": sender_id or None,
            "group_openid": group_openid or None,
            "chat_type": chat_type,
        }
    )
    context = {
        QQ_CONTEXT_KEY: exclude_none(
            {
                "scope": scope,
                "sender_id": sender_id or None,
                "group_openid": group_openid or None,
                # A click is answered through event_id, not msg_id.
                "message_id": event_reply_id(event["id"]),
            }
        )
    }
    return ChannelMessage(
        session_id=session_id,
        content=json.dumps(payload, ensure_ascii=False),
        channel=channel_name,
        chat_id=chat_id,
        is_active=True,
        context=context,
        output_channel="null" if suppress_direct_output else "",
    )


def extract_claw_cfg_update(event: dict[str, Any]) -> dict[str, Any]:
    """Pull the claw_cfg changes carried by an ``INTERACTION_UPDATE`` (2002).

    The QQ client sends the changed fields under ``resolved.claw_cfg``,
    e.g. ``{"require_mention": "mention"}`` when a group admin narrows the
    bot's message scope. Unknown ``require_mention`` values are dropped.
    """

    resolved = event.get("resolved")
    claw_cfg = resolved.get("claw_cfg") if isinstance(resolved, dict) else None
    if not isinstance(claw_cfg, dict):
        return {}
    update: dict[str, Any] = {}
    require_mention = claw_cfg.get("require_mention")
    if isinstance(require_mention, str) and require_mention in REQUIRE_MENTION_VALUES:
        update["require_mention"] = require_mention
    return update


def build_claw_cfg(
    *, require_mention: str = DEFAULT_REQUIRE_MENTION
) -> dict[str, object]:
    if require_mention not in REQUIRE_MENTION_VALUES:
        require_mention = DEFAULT_REQUIRE_MENTION
    return {
        "channel_type": "qq",
        "claw_type": "bub",
        "require_mention": require_mention,
        "group_policy": "open",
        "online_state": "online",
    }
