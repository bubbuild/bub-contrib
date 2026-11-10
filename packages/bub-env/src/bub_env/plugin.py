"""Inject environment variables from the ``env:`` config section at startup.

CLI tools and agent skill scripts spawned by Bub inherit the Bub process
environment, so keys declared in the config file become visible to them
without a ``.env`` file or shell profile exports.

Bub sets up OTLP trace export before plugins load, so an OTLP endpoint
declared here is applied by calling Bub's ``configure_otlp`` once more.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

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
    kept: list[str] = []
    for key, value in (settings.model_extra or {}).items():
        text = _coerce(value)
        if text is None:
            continue
        if key in os.environ:
            kept.append(key)
            continue
        os.environ[key] = text
        applied[key] = text
    # Names only: the values are usually secrets.
    if applied:
        logger.info("env.applied count={} keys={}", len(applied), ",".join(applied))
    if kept:
        logger.info("env.kept_process_env keys={}", ",".join(kept))
    return applied


_OTLP_ENDPOINT_KEYS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")


def _otlp_endpoint() -> str:
    """The configured endpoint without any credentials in it, for logs."""

    raw = next((os.environ[key] for key in _OTLP_ENDPOINT_KEYS if os.environ.get(key)), "")
    parts = urlsplit(raw)
    return f"{parts.scheme}://{parts.hostname or ''}{f':{parts.port}' if parts.port else ''}{parts.path}"


def _tracing_active() -> bool | None:
    """Whether a real tracer provider is installed; None without OpenTelemetry."""

    try:
        import opentelemetry.trace as otel
    except ImportError:
        return None
    return not isinstance(otel.get_tracer_provider(), otel.ProxyTracerProvider)


def configure_tracing(applied: dict[str, str]) -> None:
    """Start Bub's OTLP trace export when ``applied`` set its endpoint.

    ``configure_otlp`` does nothing without the ``bub[trace]`` extra or when
    a tracer provider is already installed, so calling it again is safe.
    It reports nothing either way, so the outcome is read back afterwards.
    """
    if not any(key in applied for key in _OTLP_ENDPOINT_KEYS):
        return
    try:
        from bub.tracing import configure_otlp
    except ImportError:  # Bub without native tracing
        logger.warning("env.tracing.skipped reason=this Bub has no native tracing (needs Bub 0.5+)")
        return
    if _tracing_active() is None:
        logger.warning("env.tracing.skipped reason=OpenTelemetry missing; install bub[trace]")
        return
    if _tracing_active():
        logger.info("env.tracing.skipped reason=a tracer provider is already installed")
        return
    try:
        configure_otlp()
    except Exception as exc:
        logger.warning("env.tracing.failed error={}", exc)
        return
    if _tracing_active():
        logger.info("env.tracing.enabled endpoint={}", _otlp_endpoint())
    else:
        logger.warning(
            "env.tracing.skipped reason=OTLP exporter unavailable or OTEL_SDK_DISABLED=true"
        )


class EnvPlugin:
    """Bub entry point; instantiated after the config file is loaded."""

    def __init__(self, framework: object | None = None) -> None:
        configure_tracing(apply_env())
