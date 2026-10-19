from __future__ import annotations

import asyncio

from bub_qq import plugin, tools


def test_qq_version_is_provided_as_command_only() -> None:
    provided = {tool.name: tool for tool in plugin.provide_tools()}
    assert provided["qq.version"].exposure == "command"
    assert provided["qq.send"].exposure == "auto"


def test_qq_version_reports_package_version() -> None:
    result = asyncio.run(tools.qq_version.run())

    assert result.startswith("bub-qq ")
