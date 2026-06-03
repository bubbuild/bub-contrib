from __future__ import annotations

import json
import shlex
from pathlib import Path

import typer

from bub_pages.config import PagesSettings, PagesStore, SiteConfig, normalize_site_path
from bub_pages.publisher import PagesPublisher
from bub_pages.server import serve_pages


def make_pages_command(settings: PagesSettings | None = None) -> typer.Typer:
    settings = settings or PagesSettings.from_env()
    store = PagesStore(settings)
    publisher = PagesPublisher(settings)
    app = typer.Typer(help="Manage language-agnostic static pages sites.")

    @app.command("list")
    def list_sites() -> None:
        """List configured pages sites."""
        config = store.read()
        if not config.sites:
            typer.echo("No Bub pages sites configured.")
            typer.echo(f"Config: {settings.config_path}")
            return

        typer.echo(_format_site_list(config.sites, publisher))
        typer.echo(f"Config: {settings.config_path}")

    @app.command("show")
    def show_site(name: str = typer.Argument(..., help="Site name.")) -> None:
        """Show one pages site configuration."""
        config = store.read()
        site = config.sites.get(name)
        if site is None:
            typer.echo(f"Bub pages site '{name}' does not exist.", err=True)
            raise typer.Exit(code=1)
        typer.echo(json.dumps(site.to_mapping(), ensure_ascii=False, indent=2))

    @app.command("add")
    def add_site(
        name: str = typer.Argument(..., help="Site name."),
        artifact: Path = typer.Argument(
            ...,
            file_okay=False,
            help="Static artifact directory to publish.",
        ),
        site_path: str | None = typer.Option(
            None,
            "--path",
            help="URL mount path. Defaults to /<name>.",
        ),
        build_dir: Path | None = typer.Option(
            None,
            "--build-dir",
            exists=True,
            file_okay=False,
            resolve_path=True,
            help="Working directory for --build. Defaults to ARTIFACT_DIR.",
        ),
        build: str | None = typer.Option(
            None,
            "--build",
            help="Optional command to refresh ARTIFACT_DIR before publishing.",
        ),
        replace: bool = typer.Option(
            False, "--replace", help="Replace an existing site."
        ),
    ) -> None:
        """Add one pages site."""
        try:
            site = SiteConfig(
                name=name,
                artifact=artifact.expanduser().resolve(strict=False),
                path=normalize_site_path(site_path, default=f"/{name}"),
                build_dir=build_dir.expanduser().resolve(strict=False)
                if build_dir
                else None,
                build=tuple(shlex.split(build)) if build else (),
            )
            store.add(site, replace=replace)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc

        typer.echo(f"Added Bub pages site '{name}'.")
        typer.echo(f"Config: {settings.config_path}")
        typer.echo(_format_single_site(site, publisher))

    @app.command("remove")
    def remove_site(
        name: str = typer.Argument(..., help="Site name."),
        purge: bool = typer.Option(
            False, "--purge", help="Delete published files too."
        ),
    ) -> None:
        """Remove one pages site."""
        try:
            store.remove(name)
        except KeyError as exc:
            typer.echo(f"Bub pages site '{name}' does not exist.", err=True)
            raise typer.Exit(code=1) from exc

        if purge:
            publisher.remove_published(name)

        typer.echo(f"Removed Bub pages site '{name}'.")
        typer.echo(f"Config: {settings.config_path}")

    @app.command("publish")
    def publish_sites(
        name: str | None = typer.Argument(
            None, help="Site name. Omit to publish all sites."
        ),
    ) -> None:
        """Publish one site, or all configured sites when name is omitted."""
        try:
            published = publisher.publish_names([name] if name else None)
        except KeyError as exc:
            missing_name = str(exc).strip("'")
            typer.echo(f"Bub pages site '{missing_name}' does not exist.", err=True)
            raise typer.Exit(code=1) from exc
        except (OSError, ValueError) as exc:
            typer.echo(f"Publish failed: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        if not published:
            typer.echo("No Bub pages sites configured.")
            return

        for site in published:
            typer.echo(
                f"Published '{site.config.name}' from {site.artifact_dir} "
                f"to {site.published_dir} at {site.config.path}"
            )

    @app.command("serve")
    def serve(
        host: str = typer.Option("127.0.0.1", "--host", help="Host to bind."),
        port: int = typer.Option(
            8000, "--port", min=1, max=65535, help="Port to bind."
        ),
        publish: bool = typer.Option(
            False, "--publish", help="Publish all sites before serving."
        ),
    ) -> None:
        """Serve published pages sites with Uvicorn and the Bub Pages ASGI app."""
        if publish:
            try:
                publisher.publish_names()
            except (OSError, ValueError) as exc:
                typer.echo(f"Publish failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc

        typer.echo(f"Serving Bub pages at http://{host}:{port}")
        typer.echo(f"Root: {settings.pages_root}")
        try:
            serve_pages(settings, host=host, port=port)
        except KeyboardInterrupt:
            typer.echo("Stopped Bub pages server.")

    return app


def _format_site_list(sites: dict[str, SiteConfig], publisher: PagesPublisher) -> str:
    lines = [typer.style("Bub Pages Sites", bold=True)]
    for name, site in sorted(sites.items(), key=lambda item: item[0]):
        lines.append(_format_single_site(site, publisher))
    return "\n".join(lines)


def _format_single_site(site: SiteConfig, publisher: PagesPublisher) -> str:
    published = publisher.published_path(site.name)
    published_text = "yes" if published.exists() else "no"
    build_dir_text = (
        str(site.build_dir) if site.build_dir is not None else str(site.artifact)
    )
    build_text = shlex.join(site.build) if site.build else "-"
    return (
        f"- {site.name}\n"
        f"  Path: {site.path}\n"
        f"  Artifact: {site.artifact}\n"
        f"  Build dir: {build_dir_text}\n"
        f"  Build: {build_text}\n"
        f"  Published: {published_text}"
    )
