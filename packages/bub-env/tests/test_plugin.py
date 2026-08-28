from __future__ import annotations

import os

import bub
from bub_env.plugin import EnvPlugin, EnvSettings, apply_env


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
