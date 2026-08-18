from __future__ import annotations

from typing import Any

from loguru import logger

INTERACTION_QUERY = 2001
INTERACTION_UPDATE = 2002

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
    return {
        "id": interaction_id,
        "type": inner_type,
        "group_openid": str(data.get("group_openid") or "").strip(),
        "resolved": resolved if isinstance(resolved, dict) else {},
    }


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
