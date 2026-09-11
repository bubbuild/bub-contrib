"""ACP web transport using the SDK's ASGI adapter and Hypercorn."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakSet

from acp.exceptions import RequestError
from acp.http.asgi import create_asgi_app
from acp.interfaces import Client
from acp.schema import InitializeResponse
from hypercorn.asyncio import serve
from hypercorn.config import Config

from bub_acp_server.agent import ACPSession, BubACPAgent
from bub_acp_server.steering import ACPSteeringInbox

if TYPE_CHECKING:
    from bub.framework import BubFramework


class _HTTPAgent(BubACPAgent):
    async def initialize(self, *args: Any, **kwargs: Any) -> InitializeResponse:
        response = await super().initialize(*args, **kwargs)
        # The SDK registers HTTP session streams from a response's sessionId,
        # which the standard LoadSessionResponse does not contain.
        response.agent_capabilities.load_session = False
        # SDK 0.12.1's HTTP adapter does not enable unstable protocol routes.
        capabilities = response.agent_capabilities.session_capabilities
        if capabilities is not None:
            capabilities.close = None
            capabilities.resume = None
        return response

    async def load_session(self, *args: Any, **kwargs: Any) -> None:
        raise RequestError.method_not_found(
            "session/load is unavailable over HTTP in SDK 0.12.1"
        )


def create_http_app(framework: BubFramework) -> Callable:
    """Create a /acp endpoint; the caller owns framework.running()."""
    prompt_lock = asyncio.Lock()
    sessions: dict[str, ACPSession] | None = None
    agents: WeakSet[BubACPAgent] = WeakSet()

    def agent_factory(client: Client) -> BubACPAgent:
        nonlocal sessions
        inbox = framework.get_steering_inbox()
        agent = _HTTPAgent(
            framework,
            steering_inbox=inbox if isinstance(inbox, ACPSteeringInbox) else None,
            prompt_lock=prompt_lock,
            sessions=sessions,
        )
        sessions = agent._sessions
        agents.add(agent)
        return agent

    sdk_app = create_asgi_app(agent_factory)

    async def app(scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "lifespan" and scope.get("path") != "/acp":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send(
                    {"type": "http.response.start", "status": 404, "headers": []}
                )
                await send({"type": "http.response.body", "body": b"Not found"})
            return
        try:
            await sdk_app(scope, receive, send)
        finally:
            if scope["type"] == "lifespan":
                tasks = [task for agent in agents for task in agent._background_tasks]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    return app


async def run_acp_http(
    framework: BubFramework,
    *,
    host: str = "127.0.0.1",
    port: int = 28200,
    certfile: Path | None = None,
    keyfile: Path | None = None,
) -> None:
    config = Config()
    config.bind = [f"{host}:{port}"]
    if certfile is not None:
        config.certfile = str(certfile)
        config.keyfile = str(keyfile) if keyfile is not None else None
    async with framework.running():
        await serve(create_http_app(framework), config)
