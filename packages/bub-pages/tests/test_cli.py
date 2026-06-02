from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from bub_pages.cli import make_pages_command
from bub_pages.config import PagesSettings, PagesStore

runner = CliRunner()


def _settings(tmp_path: Path) -> PagesSettings:
    return PagesSettings(
        config_path=tmp_path / "pages.json", pages_root=tmp_path / "pages"
    )


def test_add_list_show_and_remove_site(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = tmp_path / "website"
    source.mkdir()
    app = make_pages_command(settings)

    add_result = runner.invoke(app, ["add", "docs", str(source), "--path", "/docs"])

    assert add_result.exit_code == 0
    assert "Added Bub pages site 'docs'." in add_result.stdout
    assert PagesStore(settings).read().sites["docs"].path == "/docs"

    list_result = runner.invoke(app, ["list"])
    assert list_result.exit_code == 0
    assert "Bub Pages Sites" in list_result.stdout
    assert "Path: /docs" in list_result.stdout

    show_result = runner.invoke(app, ["show", "docs"])
    assert show_result.exit_code == 0
    assert json.loads(show_result.stdout)["path"] == "/docs"

    remove_result = runner.invoke(app, ["remove", "docs"])
    assert remove_result.exit_code == 0
    assert "Removed Bub pages site 'docs'." in remove_result.stdout
    assert PagesStore(settings).read().sites == {}


def test_publish_site_copies_static_output(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = tmp_path / "website"
    source.mkdir()
    source.joinpath("index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    app = make_pages_command(settings)

    add_result = runner.invoke(app, ["add", "docs", str(source)])
    publish_result = runner.invoke(app, ["publish", "docs"])

    assert add_result.exit_code == 0
    assert publish_result.exit_code == 0
    assert "Published 'docs'" in publish_result.stdout
    assert (
        settings.pages_root.joinpath("sites", "docs", "index.html").read_text(
            encoding="utf-8"
        )
        == "<h1>Hello</h1>"
    )


def test_publish_site_runs_optional_build_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    artifact = project / "dist"
    app = make_pages_command(settings)

    build_command = f"{sys.executable} -c \"from pathlib import Path; Path('dist').mkdir(); Path('dist/index.html').write_text('built')\""
    add_result = runner.invoke(
        app,
        [
            "add",
            "app",
            str(artifact),
            "--build-dir",
            str(project),
            "--build",
            build_command,
        ],
    )
    publish_result = runner.invoke(app, ["publish", "app"])

    assert add_result.exit_code == 0
    assert publish_result.exit_code == 0
    assert (
        settings.pages_root.joinpath("sites", "app", "index.html").read_text(
            encoding="utf-8"
        )
        == "built"
    )


def test_remove_can_purge_published_files(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = tmp_path / "website"
    source.mkdir()
    source.joinpath("index.html").write_text("hello", encoding="utf-8")
    app = make_pages_command(settings)

    runner.invoke(app, ["add", "docs", str(source)])
    runner.invoke(app, ["publish", "docs"])
    published_path = settings.pages_root / "sites" / "docs"

    assert published_path.exists()

    remove_result = runner.invoke(app, ["remove", "docs", "--purge"])

    assert remove_result.exit_code == 0
    assert not published_path.exists()


def test_publish_rejects_symlinks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = tmp_path / "website"
    source.mkdir()
    source.joinpath("outside.txt").write_text("secret", encoding="utf-8")
    source.joinpath("link.txt").symlink_to(source / "outside.txt")
    app = make_pages_command(settings)

    add_result = runner.invoke(app, ["add", "docs", str(source)])
    publish_result = runner.invoke(app, ["publish", "docs"])

    assert add_result.exit_code == 0
    assert publish_result.exit_code == 1
    assert "must not contain symlinks" in publish_result.stderr


def test_missing_site_commands_exit_with_error(tmp_path: Path) -> None:
    app = make_pages_command(_settings(tmp_path))

    show_result = runner.invoke(app, ["show", "missing"])
    publish_result = runner.invoke(app, ["publish", "missing"])
    remove_result = runner.invoke(app, ["remove", "missing"])

    assert show_result.exit_code == 1
    assert "does not exist" in show_result.stderr
    assert publish_result.exit_code == 1
    assert "does not exist" in publish_result.stderr
    assert remove_result.exit_code == 1
    assert "does not exist" in remove_result.stderr
