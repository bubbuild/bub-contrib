from __future__ import annotations

import os

import bub
import pytest
from loguru import logger

from bub_env import plugin
from bub_env.plugin import EnvPlugin, EnvSettings, apply_env, configure_tracing


def _cleanup(*keys: str) -> None:
    for key in keys:
        os.environ.pop(key, None)


def test_apply_env_injects_section_values() -> None:
    settings = EnvSettings(BUB_ENV_TEST_KEY="secret", BUB_ENV_TEST_NUM=42, BUB_ENV_TEST_FLAG=True)
    try:
        applied = apply_env(settings)
        assert applied == {
            "BUB_ENV_TEST_KEY": "secret",
            "BUB_ENV_TEST_NUM": "42",
            "BUB_ENV_TEST_FLAG": "true",
        }
        assert os.environ["BUB_ENV_TEST_KEY"] == "secret"
        assert os.environ["BUB_ENV_TEST_NUM"] == "42"
        assert os.environ["BUB_ENV_TEST_FLAG"] == "true"
    finally:
        _cleanup("BUB_ENV_TEST_KEY", "BUB_ENV_TEST_NUM", "BUB_ENV_TEST_FLAG")


def test_apply_env_does_not_override_process_env(monkeypatch) -> None:
    monkeypatch.setenv("BUB_ENV_TEST_EXISTING", "from-process")
    applied = apply_env(EnvSettings(BUB_ENV_TEST_EXISTING="from-config"))
    assert applied == {}
    assert os.environ["BUB_ENV_TEST_EXISTING"] == "from-process"


def test_apply_env_skips_null_values() -> None:
    try:
        applied = apply_env(EnvSettings(BUB_ENV_TEST_NULL=None))
        assert applied == {}
        assert "BUB_ENV_TEST_NULL" not in os.environ
    finally:
        _cleanup("BUB_ENV_TEST_NULL")


def test_settings_ignore_process_environment() -> None:
    # extra="allow" must not vacuum os.environ into the model.
    settings = EnvSettings()
    assert not settings.model_extra


def test_entry_point_class_applies_config(monkeypatch) -> None:
    monkeypatch.setattr(bub, "ensure_config", lambda cls: EnvSettings(BUB_ENV_TEST_PLUGIN="ok"))
    try:
        EnvPlugin(framework=None)
        assert os.environ["BUB_ENV_TEST_PLUGIN"] == "ok"
    finally:
        _cleanup("BUB_ENV_TEST_PLUGIN")


@pytest.fixture
def logs():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    yield lines
    logger.remove(sink)


def _fake_tracing(monkeypatch, *, active: bool | None = False, enables: bool = True) -> list[bool]:
    """Stand in for OpenTelemetry: ``active`` is the provider state before the call."""

    import bub.tracing

    state = {"active": active}
    calls: list[bool] = []

    def configure_otlp() -> None:
        calls.append(True)
        if enables:
            state["active"] = True

    monkeypatch.setattr(bub.tracing, "configure_otlp", configure_otlp)
    monkeypatch.setattr(plugin, "_tracing_active", lambda: state["active"])
    return calls


ENDPOINT = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://user:pw@phoenix.lan:6006/v1/traces"}


def test_injected_otlp_endpoint_starts_tracing(monkeypatch, logs) -> None:
    calls = _fake_tracing(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ENDPOINT["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"])
    configure_tracing(ENDPOINT)
    assert calls == [True]
    enabled = [line for line in logs if "env.tracing.enabled" in line]
    assert enabled and "http://phoenix.lan:6006/v1/traces" in enabled[0]
    assert "pw" not in enabled[0]


def test_tracing_untouched_without_injected_endpoint(monkeypatch, logs) -> None:
    calls = _fake_tracing(monkeypatch)
    # An endpoint already in the process env was handled by Bub at startup.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://phoenix:6006/v1/traces")
    configure_tracing({"OTEL_SERVICE_NAME": "bub"})
    assert calls == []
    assert not any("env.tracing" in line for line in logs)


@pytest.mark.parametrize(
    ("active", "enables", "expected"),
    [
        (None, True, "install bub[trace]"),
        (True, True, "already installed"),
        (False, False, "OTEL_SDK_DISABLED"),
    ],
)
def test_tracing_logs_why_it_did_not_start(monkeypatch, logs, active, enables, expected) -> None:
    _fake_tracing(monkeypatch, active=active, enables=enables)
    configure_tracing(ENDPOINT)
    assert any("env.tracing.skipped" in line and expected in line for line in logs)


def test_tracing_errors_do_not_break_startup(monkeypatch, logs) -> None:
    import bub.tracing

    def fail() -> None:
        raise ValueError("Bub's trace extra supports OTLP http/protobuf only.")

    _fake_tracing(monkeypatch)
    monkeypatch.setattr(bub.tracing, "configure_otlp", fail)
    configure_tracing(ENDPOINT)
    assert any("env.tracing.failed" in line and "http/protobuf" in line for line in logs)


def test_applied_keys_are_logged_without_values(monkeypatch, logs) -> None:
    monkeypatch.setenv("BUB_ENV_TEST_SET", "from-process")
    try:
        apply_env(EnvSettings(BUB_ENV_TEST_SECRET="sk-secret-value", BUB_ENV_TEST_SET="from-config"))
    finally:
        _cleanup("BUB_ENV_TEST_SECRET")
    text = "\n".join(logs)
    assert "env.applied count=1 keys=BUB_ENV_TEST_SECRET" in text
    assert "env.kept_process_env keys=BUB_ENV_TEST_SET" in text
    assert "sk-secret-value" not in text and "from-config" not in text


def test_entry_point_configures_tracing_from_config(monkeypatch) -> None:
    calls = _fake_tracing(monkeypatch)
    settings = EnvSettings(OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://phoenix:6006/v1/traces")
    monkeypatch.setattr(bub, "ensure_config", lambda cls: settings)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    try:
        EnvPlugin(framework=None)
        assert calls == [True]
    finally:
        _cleanup("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
