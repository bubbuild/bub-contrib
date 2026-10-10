"""Inject environment variables from the ``env:`` config section at startup.

CLI tools and agent skill scripts spawned by Bub inherit the Bub process
environment, so keys declared in the config file become visible to them
without a ``.env`` file or shell profile exports.

Bub sets up OTLP trace export before plugins load, so an OTLP endpoint
declared here is applied by calling Bub's ``configure_otlp`` once more.
"""

from __future__ import annotations

import os

import bub
from loguru import logger
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


@bub.config(name="env")
class EnvSettings(bub.Settings):
    """Free-form mapping of environment variable names to values.

    The whole ``env:`` section is treated as ``NAME: value`` pairs, so there
    are no declared fields; everything arrives through ``model_extra``.
    """

    model_config = SettingsConfigDict(extra="allow")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Config-file only: without this, ``extra="allow"`` would vacuum the
        # entire process environment into ``model_extra``.
        return (init_settings,)


def _coerce(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def apply_env(settings: EnvSettings | None = None) -> dict[str, str]:
    """Copy the ``env:`` section into ``os.environ``.

    Variables already present in the process environment win, matching Bub's
    usual "environment overrides config file" precedence. Returns the
    variables that were actually injected.
    """
    if settings is None:
        settings = bub.ensure_config(EnvSettings)
    applied: dict[str, str] = {}
    for key, value in (settings.model_extra or {}).items():
        text = _coerce(value)
        if text is None or key in os.environ:
            continue
        os.environ[key] = text
        applied[key] = text
    return applied


_OTLP_ENDPOINT_KEYS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")


def configure_tracing(applied: dict[str, str]) -> None:
    """Start Bub's OTLP trace export when ``applied`` set its endpoint.

    ``configure_otlp`` does nothing without the ``bub[trace]`` extra or when
    a tracer provider is already installed, so calling it again is safe.
    """
    if not any(key in applied for key in _OTLP_ENDPOINT_KEYS):
        return
    try:
        from bub.tracing import configure_otlp
    except ImportError:  # Bub without native tracing
        return
    try:
        configure_otlp()
    except Exception as exc:
        logger.warning("OTLP instrumentation disabled: {}", exc)


class EnvPlugin:
    """Bub entry point; instantiated after the config file is loaded."""

    def __init__(self, framework: object | None = None) -> None:
        configure_tracing(apply_env())
