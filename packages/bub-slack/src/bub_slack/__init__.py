"""Slack Socket Mode channel adapter for Bub."""

from __future__ import annotations

from bub_slack.channel import SlackChannel
from bub_slack.config import SlackSettings

__all__ = ["SlackChannel", "SlackSettings", "__version__"]
__version__ = "0.1.0"
