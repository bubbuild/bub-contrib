from __future__ import annotations

import asyncio

from bub.tools import REGISTRY

from bub_qq import tools


def test_qq_version_is_registered_as_command_only() -> None:
    tool = REGISTRY["qq.version"]

    assert tool.exposure == "command"
    assert REGISTRY["qq.send"].exposure == "auto"


def test_qq_version_reports_package_version() -> None:
    result = asyncio.run(tools.qq_version.run())

    assert result.startswith("bub-qq ")
