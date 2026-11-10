#!/usr/bin/env python3
"""Validate a package publish request before invoking the release workflow."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

SUBPACKAGE_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION_PATTERN = re.compile(r"[0-9][A-Za-z0-9.!+_-]*")
TITLE_PATTERN = re.compile(
    r"Request for publish: "
    rf"(?P<subpackage>{SUBPACKAGE_PATTERN.pattern}) "
    rf"(?P<version>{VERSION_PATTERN.pattern})"
)
PYPI_URL = "https://pypi.org/pypi/{subpackage}/{version}/json"


class PublishRequestError(ValueError):
    """Raised when a publish request is invalid or cannot be verified."""


@dataclass(frozen=True)
class PublishRequest:
    subpackage: str
    version: str


def parse_title(title: str) -> PublishRequest:
    match = TITLE_PATTERN.fullmatch(title.strip())
    if match is None:
        raise PublishRequestError(
            "The issue title must match "
            "`Request for publish: <package> <version>` exactly."
        )
    return PublishRequest(**match.groupdict())


def validate_request_syntax(request: PublishRequest) -> None:
    if SUBPACKAGE_PATTERN.fullmatch(request.subpackage) is None:
        raise PublishRequestError(f"Invalid package name `{request.subpackage}`.")
    if VERSION_PATTERN.fullmatch(request.version) is None:
        raise PublishRequestError(f"Invalid version value `{request.version}`.")


def validate_package(repository: Path, request: PublishRequest) -> Path:
    package_file = repository / "packages" / request.subpackage / "pyproject.toml"
    if not package_file.is_file():
        raise PublishRequestError(
            f"Package `{request.subpackage}` does not exist under `packages/`."
        )

    with package_file.open("rb") as stream:
        project = tomllib.load(stream).get("project", {})

    if project.get("name") != request.subpackage:
        raise PublishRequestError(
            f"Package directory `{request.subpackage}` does not match "
            f"project name `{project.get('name')}`."
        )
    return package_file


def validate_version_syntax(repository: Path, request: PublishRequest) -> None:
    result = subprocess.run(
        [
            "uv",
            "version",
            "--package",
            request.subpackage,
            "--dry-run",
            "--frozen",
            request.version,
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f" {detail[-1]}" if detail else ""
        raise PublishRequestError(
            f"Version `{request.version}` is not accepted by `uv version`.{suffix}"
        )


def validate_version_available(
    request: PublishRequest,
    *,
    opener: object = urllib.request.urlopen,
) -> None:
    url = PYPI_URL.format(
        subpackage=urllib.parse.quote(request.subpackage, safe=""),
        version=urllib.parse.quote(request.version, safe=""),
    )
    http_request = urllib.request.Request(
        url,
        headers={"User-Agent": "bub-contrib-publish-request/1"},
    )

    try:
        response = opener(http_request, timeout=15)  # type: ignore[operator]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise PublishRequestError(
            f"PyPI returned HTTP {error.code} while checking the requested version."
        ) from error
    except urllib.error.URLError as error:
        raise PublishRequestError(
            f"PyPI could not be reached while checking the requested version: {error.reason}"
        ) from error

    close = getattr(response, "close", None)
    if close is not None:
        close()
    raise PublishRequestError(
        f"Version `{request.version}` of `{request.subpackage}` already exists on PyPI."
    )


def validate_version(repository: Path, request: PublishRequest) -> None:
    validate_version_syntax(repository, request)
    validate_version_available(request)


def validate_request(repository: Path, request: PublishRequest) -> None:
    validate_request_syntax(request)
    validate_package(repository, request)
    validate_version(repository, request)


def write_github_outputs(
    stream: TextIO,
    *,
    valid: bool,
    request: PublishRequest | None = None,
    error: str = "",
) -> None:
    values = {
        "valid": str(valid).lower(),
        "subpackage": request.subpackage if request else "",
        "version": request.version if request else "",
        "error": " ".join(error.splitlines()),
    }
    for key, value in values.items():
        print(f"{key}={value}", file=stream)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--title", help="Publish request issue title")
    source.add_argument("--subpackage", help="Package name under packages/")
    parser.add_argument("--version", help="Version used with --subpackage")
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="Repository root",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optional GitHub Actions output file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request: PublishRequest | None = None

    try:
        if args.title is not None:
            if args.version is not None:
                raise PublishRequestError("--version cannot be used with --title.")
            request = parse_title(args.title)
        else:
            if args.version is None:
                raise PublishRequestError("--version is required with --subpackage.")
            request = PublishRequest(args.subpackage, args.version)

        validate_request(args.repository.resolve(), request)
    except (OSError, PublishRequestError, tomllib.TOMLDecodeError) as error:
        message = str(error)
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as stream:
                write_github_outputs(
                    stream,
                    valid=False,
                    request=request,
                    error=message,
                )
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error::{message}", file=sys.stderr)
        else:
            print(f"error: {message}", file=sys.stderr)
        return 1

    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            write_github_outputs(stream, valid=True, request=request)
    print(f"Validated publish request for {request.subpackage} {request.version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
