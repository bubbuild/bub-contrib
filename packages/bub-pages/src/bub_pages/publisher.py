from __future__ import annotations

import posixpath
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from bub_pages.config import PagesSettings, PagesStore, SiteConfig

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True)
class PublishedSite:
    config: SiteConfig
    artifact_dir: Path
    published_dir: Path


class PagesPublisher:
    def __init__(self, settings: PagesSettings) -> None:
        self.settings = settings
        self.store = PagesStore(settings)

    def published_path(self, site_name: str) -> Path:
        return self.settings.pages_root / "sites" / site_name

    def publish(self, site: SiteConfig) -> PublishedSite:
        if site.build:
            subprocess.run(list(site.build), cwd=_resolve_build_dir(site), check=True)  # noqa: S603

        artifact_dir = site.artifact.expanduser().resolve()
        if not artifact_dir.is_dir():
            raise ValueError(
                f"pages site '{site.name}' artifact is not a directory: {artifact_dir}"
            )
        _reject_symlinks(artifact_dir)

        published_dir = self.published_path(site.name)
        _replace_tree(artifact_dir, published_dir)
        return PublishedSite(
            config=site,
            artifact_dir=artifact_dir,
            published_dir=published_dir,
        )

    def publish_names(self, names: Iterable[str] | None = None) -> list[PublishedSite]:
        config = self.store.read()
        selected_names = list(names) if names is not None else sorted(config.sites)
        published: list[PublishedSite] = []

        for name in selected_names:
            try:
                site = config.sites[name]
            except KeyError as exc:
                raise KeyError(name) from exc
            published.append(self.publish(site))

        return published

    def remove_published(self, site_name: str) -> None:
        shutil.rmtree(self.published_path(site_name), ignore_errors=True)


def make_pages_handler(settings: PagesSettings):
    config = PagesStore(settings).read()
    routes = sorted(
        (
            (site.path, site.name, settings.pages_root / "sites" / site.name)
            for site in config.sites.values()
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    class PagesRequestHandler(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            request_path = _clean_request_path(path)
            for mount_path, _site_name, published_dir in routes:
                relative_path = _relative_request_path(request_path, mount_path)
                if relative_path is None:
                    continue
                return str(_safe_join(published_dir, relative_path))
            return str(settings.pages_root / "__missing__")

    return PagesRequestHandler


def serve_pages(settings: PagesSettings, *, host: str, port: int) -> None:
    handler = make_pages_handler(settings)
    server = ThreadingHTTPServer((host, port), handler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _resolve_build_dir(site: SiteConfig) -> Path:
    if site.build_dir is not None:
        return site.build_dir.expanduser().resolve()
    return site.artifact.expanduser().resolve()


def _replace_tree(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp_dir:
        staging = Path(tmp_dir) / target.name
        shutil.copytree(source, staging, symlinks=True)
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)


def _reject_symlinks(source: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"pages artifacts must not contain symlinks: {path}")


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
