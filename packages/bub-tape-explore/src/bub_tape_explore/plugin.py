"""Plugin entry point for tape exploration tools."""

from bub_tape_explore import tools as _tools  # noqa: F401


class _TapeExplorePlugin:
    """Side-effect-only plugin that registers tape tools on import."""


plugin = _TapeExplorePlugin()
