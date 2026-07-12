"""Slack channel configuration for Bub.

Mirrors the shipped ``bub/channels/telegram.py`` pattern:
``@config(name="slack")`` registers a pydantic ``Settings`` subclass that Bub
loads via ``ensure_config(SlackSettings)``. Bub reads YAML from
``~/.bub/config.yml`` under the ``slack:`` key, then lets ``BUB_SLACK_*`` env
vars override those values. ``bub.framework`` calls ``load_dotenv()`` at import
time, so values in a project ``.env`` are already in ``os.environ`` before any
plugin code runs.

Env prefix is ``BUB_SLACK_`` (e.g. ``BUB_SLACK_BOT_TOKEN``).
"""

from __future__ import annotations

import bub
from pydantic import Field
from pydantic_settings import SettingsConfigDict


@bub.config(name="slack")
class SlackSettings(bub.Settings):
    """Resolved Slack channel configuration."""

    # No ``env_file`` here on purpose: ``bub.framework`` calls ``load_dotenv()``
    # at import, so ``.env`` values already live in ``os.environ`` and are read
    # via the ``BUB_SLACK_`` env prefix. Omitting ``env_file`` keeps
    # ``ensure_config`` deterministic in tests — otherwise pydantic re-reads a
    # cwd-relative ``.env`` and leaks real tokens into the default-values case.
    model_config = SettingsConfigDict(env_prefix="BUB_SLACK_", extra="ignore")

    bot_token: str = Field(
        default="",
        description="Slack bot OAuth token (xoxb-...), used to post replies via the Web API.",
    )
    app_token: str = Field(
        default="",
        description="Slack app-level token (xapp-...), used to open the Socket Mode WebSocket.",
    )
    allow_channels: str | None = Field(
        default=None,
        description="Comma-separated allowed channel IDs, or empty for no restriction.",
    )
    allow_users: str | None = Field(
        default=None,
        description="Comma-separated allowed user IDs, or empty for no restriction.",
    )
