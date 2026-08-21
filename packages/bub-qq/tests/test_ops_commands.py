from __future__ import annotations

import asyncio

from bub.tools import REGISTRY

from bub_qq import tools


def test_qq_version_is_registered_as_command_only() -> None:
    tool = REGISTRY["qq.version"]

    assert tool.agent_use is False
    assert REGISTRY["qq.send"].agent_use is True


def test_qq_version_reports_package_version() -> None:
    result = asyncio.run(tools.qq_version.run())

    assert result.startswith("bub-qq ")
