"""Which Bub this plugin runs on.

bub-qq follows Bub's latest releases, but relies on interfaces that only
exist from 0.6.0 on (the ``provide_tools`` hook, comma commands running
through ``before_tool_call``, the tool names its Guard classifies). The
check runs when the plugin module loads, so an older Bub reports a clear
reason instead of failing on an unknown keyword argument.

Bub ``main`` is currently versioned by hatch-vcs from older tags, so a
locked git build can report ``0.5.x.devN+g…`` while still exposing the
0.6 interfaces. For those non-release builds the gate accepts the install
when ``BubHookSpecs.provide_tools`` is present, and keeps the existing
prerelease warning. Plain releases below 0.6.0 are still refused.
"""

from __future__ import annotations

import re

from loguru import logger

MIN_BUB_VERSION = (0, 6, 0)
# The newest minor series bub-qq was tested against; newer ones load with a
# warning, since its Guard may not know their new tools yet.
TESTED_BUB_SERIES = (0, 6)


def _has_provide_tools_hook() -> bool:
    """True when the installed Bub exposes the 0.6 ``provide_tools`` hook."""

    try:
        from bub.hooks import BubHookSpecs
    except ImportError:
        return False
    return hasattr(BubHookSpecs, "provide_tools")


def check_bub_version(version: str) -> None:
    """Refuse a Bub older than :data:`MIN_BUB_VERSION`; warn on untested ones."""

    minimum = ".".join(map(str, MIN_BUB_VERSION))
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    parsed = (
        (int(match[1]), int(match[2]), int(match[3] or 0))
        if match is not None
        else None
    )
    release = version.split("+", 1)[0]
    is_non_release = re.fullmatch(r"\d+(\.\d+)*", release) is None

    if parsed is None or parsed < MIN_BUB_VERSION:
        # hatch-vcs on bub main can report 0.5.x.dev while the tree already
        # has provide_tools; accept that class of install by capability.
        if parsed is not None and is_non_release and _has_provide_tools_hook():
            logger.warning(
                "qq.bub.prerelease bub={} is a development or pre-release build; if"
                " bub-qq misbehaves, install a Bub release (bub>={})",
                version,
                minimum,
            )
            return
        raise RuntimeError(
            f"bub-qq needs Bub >= {minimum}, but Bub {version} is installed"
        )

    if is_non_release:
        logger.warning(
            "qq.bub.prerelease bub={} is a development or pre-release build; if"
            " bub-qq misbehaves, install a Bub release (bub>={})",
            version,
            minimum,
        )
    elif (parsed[0], parsed[1]) > TESTED_BUB_SERIES:
        logger.warning(
            "qq.bub.untested bub={} is newer than the Bub {}.x bub-qq was tested"
            " with; tools it adds are admin-only until bub-qq knows them",
            version,
            ".".join(map(str, TESTED_BUB_SERIES)),
        )
