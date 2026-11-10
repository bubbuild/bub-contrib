"""Persistent QQ channel state that must survive restarts.

One small JSON file, written atomically, holds:

- admins registered with ``,qq.claim`` (merged with ``admin_users``);
- whether proactive (active) messages are allowed, toggled from the QQ
  client (``GROUP_MSG_RECEIVE`` / ``GROUP_MSG_REJECT`` and the C2C
  equivalents);
- each group's message-scope setting from ``claw_cfg`` interactions.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from .config import QQConfig

_SECTION_BY_SCOPE = {"group": "groups", "c2c": "users"}


def resolve_state_path(config: QQConfig) -> Path:
    """``state_file`` from config, or ``<bub home>/qq/state.json``."""

    raw = (config.state_file or "").strip()
    if raw:
        return Path(raw).expanduser()
    import bub

    return bub.home / "qq" / "state.json"


class QQPlatformStore:
    """JSON-file-backed key-value store, loaded eagerly, written atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, dict[str, Any]]] = {
            "groups": {},
            "users": {},
            "admins": {},
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

    def admins(self) -> frozenset[str]:
        """Identities registered as admins with ``,qq.claim``."""

        return frozenset(self._data["admins"])

    def add_admin(self, identity: str, **fields: Any) -> None:
        self._data["admins"][identity] = dict(fields)
        self._save()

    def remove_admin(self, identity: str) -> bool:
        if self._data["admins"].pop(identity, None) is None:
            return False
        self._save()
        return True

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
