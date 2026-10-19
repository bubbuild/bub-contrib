from __future__ import annotations

import pytest

from bub_qq import admins, approval, security


@pytest.fixture(autouse=True)
def _default_command_prefix(monkeypatch):
    # Many tests replace bub.ensure_config with one returning a QQConfig for
    # any class, which would also answer Bub's AgentSettings lookup behind
    # command_prefix(). Pin Bub's default prefix; tests of other prefixes
    # override it.
    for module in (security, approval, admins):
        monkeypatch.setattr(module, "command_prefix", lambda: ",")


@pytest.fixture(scope="session")
def agent_tools() -> dict:
    """The tools Bub's agent gets from the builtin hooks and this plugin."""

    from bub.framework import BubFramework

    from bub_qq import plugin

    framework = BubFramework()
    framework.load_builtin_hooks()
    framework.plugin_manager.register(plugin, name="qq")
    return {tool.name: tool for tool in framework.get_tools()}
