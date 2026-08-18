"""Persistent platform-controlled switches for the QQ channel.

Group owners/admins toggle two things from the QQ client that the bot
must remember across restarts: whether proactive (active) messages are
allowed (``GROUP_MSG_RECEIVE`` / ``GROUP_MSG_REJECT`` and the C2C
equivalents) and the group's message-scope setting delivered through
``claw_cfg`` interaction updates. This store keeps both in one small
JSON file with atomic writes.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

_SECTION_BY_SCOPE = {"group": "groups", "c2c": "users"}


class QQPlatformStore:
    """JSON-file-backed key-value store, loaded eagerly, written atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, dict[str, Any]]] = {
            "groups": {},
            "users": {},
        }
        self._load()

    def get(self, scope: str, openid: str) -> dict[str, Any]:
        """Return a copy of the stored entry for one group/user."""

        return dict(self._data[_section(scope)].get(openid) or {})

    def update(self, scope: str, openid: str, **fields: Any) -> None:
        """Merge ``fields`` into one entry and persist the store."""

        section = self._data[_section(scope)]
        entry = section.setdefault(openid, {})
        entry.update(fields)
        self._save()

    def active_messages_allowed(self, scope: str, openid: str) -> bool | None:
        """Platform opt-in state for active messages; ``None`` when unknown."""

        value = self.get(scope, openid).get("active_messages")
        return value if isinstance(value, bool) else None

    def require_mention(self, group_openid: str) -> str | None:
        """Persisted claw_cfg ``require_mention`` value for one group."""

        value = self.get("group", group_openid).get("require_mention")
        return value if isinstance(value, str) and value else None

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("qq.store.load_failed path={} error={}", self._path, exc)
            return
        if not isinstance(raw, dict):
            logger.warning("qq.store.load_failed path={} error=not_an_object", self._path)
            return
        for section_name in self._data:
            section = raw.get(section_name)
            if isinstance(section, dict):
                self._data[section_name] = {
                    str(openid): dict(entry)
                    for openid, entry in section.items()
                    if isinstance(entry, dict)
                }

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent),
                prefix=f"{self._path.name}.",
                suffix=".tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.warning("qq.store.save_failed path={} error={}", self._path, exc)


def _section(scope: str) -> str:
    section = _SECTION_BY_SCOPE.get(scope)
    if section is None:
        raise ValueError(f"unknown qq store scope: {scope!r}")
    return section
