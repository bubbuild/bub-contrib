from __future__ import annotations

import asyncio

from bub.framework import BubFramework

from bub_acp_server.plugin import run_acp_agent


def main() -> None:
    framework = BubFramework()
    framework.load_hooks()
    asyncio.run(run_acp_agent(framework))
