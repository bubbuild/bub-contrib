from __future__ import annotations

import pytest
from bub import configure
from loguru import logger

from bub_qq.channel import QQChannel
from bub_qq.bub_version import check_bub_version


@pytest.fixture
def make_channel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def build(**qq: object) -> QQChannel:
        configure._config_data.clear()
        configure.merge(
            configure._config_data,
            {
                "qq": {
                    "receive_mode": "webhook",
                    "state_file": str(tmp_path / "state.json"),
                    **qq,
                }
            },
        )
        configure._global_config.clear()
        return QQChannel(lambda message: None)

    yield build
    configure._global_config.clear()
    configure._config_data.clear()


@pytest.fixture
def logs():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    yield lines
    logger.remove(sink)


def test_allow_users_access_needs_a_list(make_channel) -> None:
    with pytest.raises(RuntimeError, match="allow_users"):
        make_channel(c2c_access="allow_users")._check_security_config()
    make_channel(c2c_access="allow_users", allow_users="u1")._check_security_config()


def test_removed_options_and_unsandboxed_shell_warn(make_channel, logs) -> None:
    make_channel(exec_approval=True, workspace_jail=False)._check_security_config()
    text = "\n".join(logs)
    assert "option=exec_approval is ignored" in text
    assert "option=workspace_jail is ignored" in text
    assert "qq.security.shell_unsandboxed" in text


def test_quiet_when_shell_is_off_or_sandboxed(make_channel, logs) -> None:
    make_channel(group_shell="deny")._check_security_config()
    assert not any("shell_unsandboxed" in line for line in logs)
    make_channel(shell_sandbox="external")._check_security_config()
    assert not any("shell_unsandboxed" in line for line in logs)
    assert any("qq.security.shell_sandbox" in line for line in logs)


def test_unclaimed_bot_logs_a_pairing_code(make_channel, logs) -> None:
    code = make_channel()._admins.bootstrap_code()
    assert code is not None
    assert any(f",qq.claim {code}" in line for line in logs)
    assert make_channel(admin_users="c2c:u1")._admins.bootstrap_code() is None


def test_send_services_get_platform_limits_per_scope(make_channel) -> None:
    channel = make_channel()
    assert channel._c2c_send._passive_reply_window_seconds == 3600
    assert channel._c2c_send._passive_replies_per_msg_id == 4
    assert channel._group_send._passive_reply_window_seconds == 300
    assert channel._group_send._passive_replies_per_msg_id == 5

    tuned = make_channel(passive_reply_window_seconds=120, passive_replies_per_msg_id=2)
    assert tuned._group_send._passive_reply_window_seconds == 120
    assert tuned._c2c_send._passive_replies_per_msg_id == 2


@pytest.mark.parametrize("version", ["0.6.0", "0.6.9", "0.6.2+local"])
def test_tested_bub_releases_load_quietly(version, logs) -> None:
    check_bub_version(version)
    assert not any("qq.bub." in line for line in logs)


@pytest.mark.parametrize("version", ["0.6.0a4.dev1+g225a39898", "0.6.2rc1", "0.6.0a4"])
def test_bub_dev_builds_load_with_a_warning(version, logs) -> None:
    check_bub_version(version)
    assert any("qq.bub.prerelease" in line and version in line for line in logs)


@pytest.mark.parametrize(
    "version",
    ["0.5.2.dev21+ga93f5b80c", "0.5.1.dev3+gabcdef0"],
)
def test_locked_main_dev_builds_load_when_provide_tools_exists(version, logs) -> None:
    # hatch-vcs on bub main can report 0.5.x.dev while the tree has 0.6 hooks.
    check_bub_version(version)
    assert any("qq.bub.prerelease" in line and version in line for line in logs)


def test_below_minimum_dev_build_without_provide_tools_is_refused(monkeypatch) -> None:
    monkeypatch.setattr("bub_qq.bub_version._has_provide_tools_hook", lambda: False)
    with pytest.raises(RuntimeError, match=r"needs Bub >= 0\.6\.0"):
        check_bub_version("0.5.2.dev21+ga93f5b80c")


@pytest.mark.parametrize("version", ["0.7.0", "1.0.0"])
def test_newer_bub_series_load_with_a_warning(version, logs) -> None:
    # Follows Bub's latest releases: newer series are not refused.
    check_bub_version(version)
    assert any("qq.bub.untested" in line and version in line for line in logs)


@pytest.mark.parametrize("version", ["0.4.2", "0.5.9", "unknown"])
def test_bub_older_than_the_minimum_is_refused(version) -> None:
    with pytest.raises(RuntimeError, match=r"needs Bub >= 0\.6\.0"):
        check_bub_version(version)
