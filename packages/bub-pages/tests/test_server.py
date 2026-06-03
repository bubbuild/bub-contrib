from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bub_pages.config import PagesConfig, PagesSettings, PagesStore, SiteConfig
from bub_pages.server import PagesASGIApp


def _settings(tmp_path: Path) -> PagesSettings:
    return PagesSettings(
        config_path=tmp_path / "pages.json", pages_root=tmp_path / "pages"
    )


def _write_site(settings: PagesSettings, name: str, path: str, body: str) -> None:
    published_dir = settings.pages_root / "sites" / name
    published_dir.mkdir(parents=True, exist_ok=True)
    published_dir.joinpath("index.html").write_text(body, encoding="utf-8")
    published_dir.joinpath("asset.txt").write_text(f"{name} asset", encoding="utf-8")


def _configure_sites(settings: PagesSettings) -> None:
    PagesStore(settings).write(
        PagesConfig(
            sites={
                "docs": SiteConfig(
                    name="docs",
                    artifact=Path("/artifact/docs"),
                    path="/docs",
                ),
                "app": SiteConfig(
                    name="app",
                    artifact=Path("/artifact/app"),
                    path="/",
                ),
            }
        )
    )


def _call_app(
    app: PagesASGIApp, *, path: str, method: str = "GET"
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(
        app(
            {"type": "http", "method": method, "path": path},
            receive,
            send,
        )
    )
    return messages


def _response_body(messages: list[dict[str, Any]]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )


def test_asgi_app_serves_longest_matching_site_route(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _configure_sites(settings)
    _write_site(settings, "docs", "/docs", "docs index")
    _write_site(settings, "app", "/", "root index")
    app = PagesASGIApp(settings)

    messages = _call_app(app, path="/docs/")

    assert messages[0]["status"] == 200
    assert _response_body(messages) == b"docs index"


def test_asgi_app_serves_root_site_when_no_longer_route_matches(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _configure_sites(settings)
    _write_site(settings, "docs", "/docs", "docs index")
    _write_site(settings, "app", "/", "root index")
    app = PagesASGIApp(settings)

    messages = _call_app(app, path="/asset.txt")

    assert messages[0]["status"] == 200
    assert _response_body(messages) == b"app asset"


def test_asgi_app_rejects_path_traversal(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _configure_sites(settings)
    _write_site(settings, "docs", "/docs", "docs index")
    app = PagesASGIApp(settings)

    messages = _call_app(app, path="/docs/../app/index.html")

    assert messages[0]["status"] == 404


def test_asgi_app_handles_head_without_body(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _configure_sites(settings)
    _write_site(settings, "docs", "/docs", "docs index")
    app = PagesASGIApp(settings)

    messages = _call_app(app, path="/docs/asset.txt", method="HEAD")

    assert messages[0]["status"] == 200
    assert _response_body(messages) == b""


def test_asgi_app_rejects_unsupported_methods(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _configure_sites(settings)
    app = PagesASGIApp(settings)

    messages = _call_app(app, path="/docs/", method="POST")

    assert messages[0]["status"] == 405


def test_asgi_app_handles_lifespan(tmp_path: Path) -> None:
    app = PagesASGIApp(_settings(tmp_path))
    messages: list[dict[str, Any]] = []
    inbound = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )

    async def receive() -> dict[str, Any]:
        return next(inbound)

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app({"type": "lifespan"}, receive, send))

    assert messages == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
