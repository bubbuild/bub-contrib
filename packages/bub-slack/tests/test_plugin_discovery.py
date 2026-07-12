from __future__ import annotations

import importlib.metadata

import bub_slack.plugin as plugin_mod
from bub.configure import CONFIG_MAP
from bub_slack.channel import SlackChannel


def test_provide_channels_returns_one_slack_channel() -> None:
    sentinel = object()
    channels = plugin_mod.provide_channels(sentinel)  # type: ignore[arg-type]
    assert len(channels) == 1
    ch = channels[0]
    assert isinstance(ch, SlackChannel)
    assert ch.name == "slack"
    assert ch._on_receive is sentinel


def test_importing_plugin_registers_slack_settings() -> None:
    # The eager ``from . import config`` in plugin.py registers SlackSettings
    # under the "slack" config section at import time.
    assert "slack" in CONFIG_MAP
    registered = CONFIG_MAP["slack"]
    # CONFIG_MAP maps section name -> list of registered settings classes.
    classes = registered if isinstance(registered, list) else [registered]
    assert any(c.__name__ == "SlackSettings" for c in classes)


def test_entry_point_registered() -> None:
    # Only meaningful once the package is installed (editable or otherwise).
    try:
        eps = importlib.metadata.entry_points(group="bub")
    except Exception:  # pragma: no cover
        return
    names = [ep.name for ep in eps]
    if "slack" in names:
        ep = next(e for e in eps if e.name == "slack")
        assert ep.value == "bub_slack.plugin"
