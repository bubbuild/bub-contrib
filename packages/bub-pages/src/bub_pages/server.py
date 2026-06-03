from __future__ import annotations

import mimetypes
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from bub_pages.config import PagesSettings, PagesStore

FILE_CHUNK_SIZE = 64 * 1024

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
    ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class PagesRoute:
    mount_path: str
    site_name: str
    published_dir: Path


class PagesASGIApp:
    def __init__(self, settings: PagesSettings) -> None:
        self.settings = settings
        self.routes = _load_routes(settings)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope["type"] == "lifespan":
            await _handle_lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(f"unsupported ASGI scope type: {scope['type']}")

        method = str(scope.get("method", "GET")).upper()
        if method not in {"GET", "HEAD"}:
            await _send_text(send, 405, "Method Not Allowed")
            return

        file_path = self.resolve(scope.get("path", "/"))
        if file_path is None:
            await _send_text(send, 404, "Not Found")
            return

        await _send_file(send, file_path, include_body=method == "GET")

    def resolve(self, path: object) -> Path | None:
        request_path = _clean_request_path(str(path))
        for route in self.routes:
            relative_path = _relative_request_path(request_path, route.mount_path)
            if relative_path is None:
                continue
            file_path = _safe_join(route.published_dir, relative_path)
            if file_path.is_dir():
                file_path = file_path / "index.html"
            if file_path.is_file():
                return file_path
        return None


def make_pages_app(settings: PagesSettings) -> PagesASGIApp:
    return PagesASGIApp(settings)


def serve_pages(settings: PagesSettings, *, host: str, port: int) -> None:
    import uvicorn

    uvicorn.run(make_pages_app(settings), host=host, port=port, factory=False)


def _load_routes(settings: PagesSettings) -> list[PagesRoute]:
    config = PagesStore(settings).read()
    routes = [
        PagesRoute(
            mount_path=site.path,
            site_name=site.name,
            published_dir=settings.pages_root / "sites" / site.name,
        )
        for site in config.sites.values()
    ]
    return sorted(routes, key=lambda route: len(route.mount_path), reverse=True)


async def _send_file(send: ASGISend, file_path: Path, *, include_body: bool) -> None:
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    headers = [
        (b"content-type", media_type.encode("ascii")),
        (b"content-length", str(file_path.stat().st_size).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    if not include_body:
        await send({"type": "http.response.body", "body": b""})
        return

    with file_path.open("rb") as file:
        while chunk := file.read(FILE_CHUNK_SIZE):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                }
            )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _send_text(send: ASGISend, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _handle_lifespan(receive: ASGIReceive, send: ASGISend) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def _clean_request_path(path: str) -> str:
    raw_path = unquote(urlsplit(path).path)
    normalized = posixpath.normpath(raw_path)
    if normalized == ".":
        return "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _relative_request_path(request_path: str, mount_path: str) -> str | None:
    if mount_path == "/":
        return request_path.removeprefix("/")
    if request_path == mount_path:
        return ""
    prefix = f"{mount_path}/"
    if request_path.startswith(prefix):
        return request_path.removeprefix(prefix)
    return None


def _safe_join(root: Path, relative_path: str) -> Path:
    parts = [
        part for part in relative_path.split("/") if part and part not in {".", ".."}
    ]
    return root.joinpath(*parts)
