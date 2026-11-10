"""AG-UI request/response transport; conversation storage belongs to the host."""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from contextlib import aclosing, asynccontextmanager, nullcontext
from typing import Any

import bub
from ag_ui.core import RunAgentInput
from ag_ui.encoder import EventEncoder
from hypercorn.asyncio import serve
from hypercorn.config import Config
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from bub_ag_ui.config import AGUISettings
from bub_ag_ui.events import EVENT
from bub_ag_ui.runtime import AGUIAdapter, ProtocolError

# Authentication must return a stable, trusted tenant/user identity. Never use
# a browser-supplied threadId or a forwardedProps field as the principal.
type Authenticate = Callable[[Request], Awaitable[str]]


def create_app(
    framework: Any,
    *,
    settings: AGUISettings | None = None,
    adapter: AGUIAdapter | None = None,
    authenticate: Authenticate | None = None,
    manage_framework: bool = True,
    channel_managed: bool = False,
) -> Any:
    """Create the /agent endpoint; its lifespan owns the framework by default."""
    settings = settings or bub.ensure_config(AGUISettings)
    runtime = adapter

    @asynccontextmanager
    async def lifespan(app: Starlette):
        nonlocal runtime
        async with framework.running() if manage_framework else nullcontext():
            runtime = runtime or AGUIAdapter(
                framework,
                max_run_seconds=settings.max_run_seconds,
                bind_router=not channel_managed,
            )
            try:
                yield
            finally:
                await runtime.close()

    async def principal(request: Request) -> str:
        if authenticate is not None:
            value = await authenticate(request)
            if not value:
                raise ProtocolError("Authentication required", 401)
            return value
        if settings.token is not None:
            expected = "Bearer " + settings.token.get_secret_value()
            if not hmac.compare_digest(
                request.headers.get("authorization", ""), expected
            ):
                raise ProtocolError("Authentication required", 401)
            return "token-user"
        if settings.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ProtocolError("Remote access requires authentication", 403)
        # Also guard custom ASGI hosting/proxies, not just the CLI listen address.
        if request.client is None or request.client.host not in {
            "127.0.0.1",
            "::1",
            "localhost",
            "testclient",
        }:
            raise ProtocolError("Remote access requires authentication", 403)
        return "local"

    def controller() -> AGUIAdapter:
        if runtime is None:
            raise RuntimeError("ASGI lifespan must start before serving requests")
        return runtime

    async def run(request: Request):
        user = await principal(request)
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > settings.max_body_bytes:
                raise ProtocolError("Request too large", 413)
        try:
            data = RunAgentInput.model_validate_json(bytes(content))
        except ValidationError as exc:
            raise ProtocolError("Invalid RunAgentInput", 422) from exc
        encoder = EventEncoder()

        async def body():
            async with aclosing(controller().run(user, data)) as events:
                async for event in events:
                    yield encoder.encode(EVENT.validate_python(event))

        return StreamingResponse(
            body(),
            media_type=encoder.get_content_type(),
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    async def error(request: Request, exc: ProtocolError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)

    app = Starlette(
        routes=[Route("/agent", run, methods=["POST"])],
        lifespan=lifespan,
        exception_handlers={ProtocolError: error},
    )
    return CORSMiddleware(
        app,
        allow_origins=settings.origins,
        allow_methods=["POST"],
        allow_headers=["Content-Type", "Authorization"],
        expose_headers=["Content-Type"],
    )


async def run_server(
    framework: Any,
    *,
    settings: AGUISettings,
    channel_managed: bool = False,
    shutdown_trigger: Callable[[], Awaitable[None]] | None = None,
) -> None:
    if (
        settings.host not in {"127.0.0.1", "localhost", "::1"}
        and settings.token is None
    ):
        raise ValueError(
            "Set BUB_AG_UI_TOKEN before listening on a non-loopback address"
        )
    config = Config()
    config.bind = [f"{settings.host}:{settings.port}"]
    await serve(
        create_app(
            framework,
            settings=settings,
            manage_framework=not channel_managed,
            channel_managed=channel_managed,
        ),
        config,
        shutdown_trigger=shutdown_trigger,
    )
