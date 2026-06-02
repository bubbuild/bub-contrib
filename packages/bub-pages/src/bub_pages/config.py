from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SITE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class SiteConfig:
    name: str
    artifact: Path
    path: str
    build_dir: Path | None = None
    build: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", validate_site_name(self.name))
        object.__setattr__(
            self,
            "path",
            normalize_site_path(self.path, default=f"/{self.name}"),
        )

    @classmethod
    def from_mapping(cls, name: str, payload: Any) -> SiteConfig:
        if not isinstance(payload, dict):
            raise ValueError(f"site '{name}' must be an object")

        artifact = _required_text(payload, "artifact", site_name=name)
        path = payload.get("path", f"/{name}")
        if not isinstance(path, str):
            raise ValueError(f"site '{name}' field 'path' must be a string")

        build_dir = payload.get("build_dir")
        if build_dir is not None and not isinstance(build_dir, str):
            raise ValueError(f"site '{name}' field 'build_dir' must be a string")

        build = payload.get("build", [])
        if not isinstance(build, list) or not all(
            isinstance(item, str) for item in build
        ):
            raise ValueError(f"site '{name}' field 'build' must be a list of strings")

        return cls(
            name=name,
            artifact=Path(artifact).expanduser(),
            path=normalize_site_path(path, default=f"/{name}"),
            build_dir=Path(build_dir).expanduser() if build_dir else None,
            build=tuple(build),
        )

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "artifact": str(self.artifact),
            "path": self.path,
        }
        if self.build_dir is not None:
            payload["build_dir"] = str(self.build_dir)
        if self.build:
            payload["build"] = list(self.build)
        return payload


@dataclass
class PagesConfig:
    sites: dict[str, SiteConfig] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Any) -> PagesConfig:
        if not isinstance(payload, dict):
            raise ValueError("pages config must be an object")

        raw_sites = payload.get("sites", {})
        if not isinstance(raw_sites, dict):
            raise ValueError("pages config field 'sites' must be an object")

        sites = {
            name: SiteConfig.from_mapping(name, site_payload)
            for name, site_payload in raw_sites.items()
        }
        _validate_unique_paths(sites)
        return cls(sites=sites)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "sites": {
                name: site.to_mapping()
                for name, site in sorted(self.sites.items(), key=lambda item: item[0])
            }
        }


@dataclass
class PagesSettings:
    config_path: Path
    pages_root: Path

    @classmethod
    def from_env(cls) -> PagesSettings:
        bub_home = Path(os.environ.get("BUB_HOME", Path.home() / ".bub")).expanduser()
        config_path = Path(
            os.environ.get("BUB_PAGES_CONFIG", bub_home / "pages.json")
        ).expanduser()
        pages_root = Path(
            os.environ.get("BUB_PAGES_ROOT", bub_home / "pages")
        ).expanduser()
        return cls(config_path=config_path, pages_root=pages_root)


class PagesStore:
    def __init__(self, settings: PagesSettings) -> None:
        self.settings = settings

    def read(self) -> PagesConfig:
        try:
            raw_text = self.settings.config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return PagesConfig()

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"pages config must contain valid JSON: {exc.msg}"
            ) from exc

        return PagesConfig.from_mapping(payload)

    def write(self, config: PagesConfig) -> None:
        self.settings.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.config_path.write_text(
            json.dumps(config.to_mapping(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def add(self, site: SiteConfig, *, replace: bool = False) -> None:
        config = self.read()
        if site.name in config.sites and not replace:
            raise ValueError(f"pages site '{site.name}' already exists")

        next_sites = dict(config.sites)
        next_sites[site.name] = site
        next_config = PagesConfig(sites=next_sites)
        _validate_unique_paths(next_config.sites)
        self.write(next_config)

    def remove(self, name: str) -> SiteConfig:
        config = self.read()
        try:
            site = config.sites.pop(name)
        except KeyError as exc:
            raise KeyError(name) from exc
        self.write(config)
        return site


def validate_site_name(name: str) -> str:
    if not SITE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "site name must start with a letter or digit and contain only letters, "
            "digits, dots, underscores, or dashes"
        )
    return name


def normalize_site_path(path: str | None, *, default: str) -> str:
    raw_path = (path or default).strip()
    if not raw_path:
        raw_path = default
    if not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"
    normalized = "/" + "/".join(part for part in raw_path.split("/") if part)
    return normalized if normalized != "" else "/"


def _required_text(payload: dict[str, Any], key: str, *, site_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"site '{site_name}' field '{key}' must be a non-empty string")
    return value


def _validate_unique_paths(sites: dict[str, SiteConfig]) -> None:
    seen: dict[str, str] = {}
    for name, site in sites.items():
        if existing_name := seen.get(site.path):
            raise ValueError(
                f"pages sites '{existing_name}' and '{name}' both use path '{site.path}'"
            )
        seen[site.path] = name
