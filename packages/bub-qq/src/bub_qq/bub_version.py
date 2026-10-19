"""Which Bub this plugin runs on.

bub-qq follows Bub's latest releases, but relies on interfaces that only
exist from 0.6.0 on (the ``provide_tools`` hook, comma commands running
through ``before_tool_call``, the tool names its Guard classifies). The
check runs when the plugin module loads, so an older Bub reports a clear
reason instead of failing on an unknown keyword argument.
"""

from __future__ import annotations

import re

from loguru import logger

MIN_BUB_VERSION = (0, 6, 0)
# The newest minor series bub-qq was tested against; newer ones load with a
# warning, since its Guard may not know their new tools yet.
TESTED_BUB_SERIES = (0, 6)


def check_bub_version(version: str) -> None:
    """Refuse a Bub older than :data:`MIN_BUB_VERSION`; warn on untested ones."""

    minimum = ".".join(map(str, MIN_BUB_VERSION))
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if match is None or (int(match[1]), int(match[2]), int(match[3] or 0)) < MIN_BUB_VERSION:
        raise RuntimeError(f"bub-qq needs Bub >= {minimum}, but Bub {version} is installed")
    release = version.split("+", 1)[0]
    if re.fullmatch(r"\d+(\.\d+)*", release) is None:
        logger.warning(
            "qq.bub.prerelease bub={} is a development or pre-release build; if"
            " bub-qq misbehaves, install a Bub release (bub>={})",
            version,
            minimum,
        )
    elif (int(match[1]), int(match[2])) > TESTED_BUB_SERIES:
        logger.warning(
            "qq.bub.untested bub={} is newer than the Bub {}.x bub-qq was tested"
            " with; tools it adds are admin-only until bub-qq knows them",
            version,
            ".".join(map(str, TESTED_BUB_SERIES)),
        )
