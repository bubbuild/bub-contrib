from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bub import configure
from bub.channels.message import ChannelMessage

from bub_qq.admins import GROUP_CODE_TTL_SECONDS
from bub_qq.admins import LOCKOUT_SECONDS
from bub_qq.admins import MAX_FAILURES
from bub_qq.admins import AdminRegistry
from bub_qq.config import QQConfig
from bub_qq.guard import Requester
from bub_qq.guard import is_admin
from bub_qq.guard import set_registered_admins
from bub_qq.store import QQPlatformStore

OWNER_C2C = Requester(scope="c2c", sender_id="u-owner")
OWNER_IN_GROUP = Requester(scope="group", sender_id="m-owner", group_openid="g1")
STRANGER = Requester(scope="c2c", sender_id="u-stranger")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _reset():
    set_registered_admins(())
    yield
    set_registered_admins(())


@pytest.fixture
def env(tmp_path: Path):
    config = QQConfig.model_construct(admin_users="")
    store = QQPlatformStore(tmp_path / "state.json")
    clock = Clock()
    return AdminRegistry(store, config, clock=clock), config, store, clock


def test_bootstrap_claim_in_c2c(env) -> None:
    registry, config, store, _ = env
    code = registry.bootstrap_code()
    assert code is not None and len(code) == 10
    assert registry.bootstrap_code() == code  # stable until claimed/expired

    reply = registry.handle(f",qq.claim {code.lower()}", OWNER_C2C)

    assert reply == "已登记为管理员：c2c:u-owner"
    assert store.admins() == {"c2c:u-owner"}
    assert is_admin(config, OWNER_C2C)
    assert registry.bootstrap_code() is None
    # Single use.
    assert registry.handle(f",qq.claim {code}", STRANGER) == "配对码无效或已过期。"
    assert not is_admin(config, STRANGER)


def test_bootstrap_code_sent_in_group_is_burned(env) -> None:
    registry, config, _, _ = env
    code = registry.bootstrap_code()

    reply = registry.handle(f",qq.claim {code}", OWNER_IN_GROUP)

    assert "只能在单聊中使用" in reply
    assert not is_admin(config, OWNER_IN_GROUP)
    fresh = registry.bootstrap_code()
    assert fresh is not None and fresh != code
    assert registry.handle(f",qq.claim {code}", OWNER_C2C) == "配对码无效或已过期。"


def test_bootstrap_code_expires(env) -> None:
    registry, _, _, clock = env
    code = registry.bootstrap_code()
    clock.now += 601
    assert registry.handle(f",qq.claim {code}", OWNER_C2C) == "配对码无效或已过期。"
    assert registry.bootstrap_code() != code


def test_failed_attempts_lock_the_sender(env) -> None:
    registry, config, _, clock = env
    code = registry.bootstrap_code()
    replies = [registry.handle(",qq.claim WRONGCODE1", STRANGER) for _ in range(MAX_FAILURES)]
    # Told once, on the attempt that locks; then nothing, so the bot cannot
    # be made to flood the chat. A valid code does not help while locked.
    assert replies[:-1] == ["配对码无效或已过期。"] * (MAX_FAILURES - 1)
    assert "尝试次数过多" in replies[-1]
    assert registry.handle(f",qq.claim {code}", STRANGER) == ""
    assert registry.handle(",qq.claim WRONGCODE2", STRANGER) == ""
    assert not is_admin(config, STRANGER)
    clock.now += LOCKOUT_SECONDS + 1
    code = registry.bootstrap_code()
    assert registry.handle(f",qq.claim {code}", STRANGER).startswith("已登记")


def test_group_identity_registration(env) -> None:
    registry, config, store, clock = env
    registry.handle(f",qq.claim {registry.bootstrap_code()}", OWNER_C2C)

    assert registry.handle(",qq.claim group", STRANGER) == "只有管理员可以生成群登记码。"
    assert "单聊" in registry.handle(",qq.claim group", OWNER_IN_GROUP)

    reply = registry.handle(",qq.claim group", OWNER_C2C)
    group_code = reply.split("：", 1)[1].split("\n", 1)[0]
    assert "群里发送" in registry.handle(f",qq.claim {group_code}", OWNER_C2C)
    assert registry.handle(f",qq.claim {group_code}", OWNER_IN_GROUP).startswith("已登记")
    assert "group:g1:m-owner" in store.admins()
    assert is_admin(config, OWNER_IN_GROUP)
    # Registration in one group does not cover another group.
    assert not is_admin(config, Requester("group", "m-owner", "g2"))

    reply = registry.handle(",qq.claim group", OWNER_C2C)
    expired = reply.split("：", 1)[1].split("\n", 1)[0]
    clock.now += GROUP_CODE_TTL_SECONDS + 1
    other = Requester("group", "m-owner", "g2")
    assert registry.handle(f",qq.claim {expired}", other) == "配对码无效或已过期。"


def test_admins_list_and_remove(tmp_path: Path) -> None:
    config = QQConfig.model_construct(admin_users="c2c:u-config")
    store = QQPlatformStore(tmp_path / "state.json")
    store.add_admin("group:g1:m-owner")
    registry = AdminRegistry(store, config, clock=Clock())
    admin = Requester("c2c", "u-config")

    assert registry.handle(",qq.admins", STRANGER) is None  # not an admin: plain text
    listing = registry.handle(",qq.admins", admin)
    assert "c2c:u-config（配置）" in listing and "group:g1:m-owner（已登记）" in listing
    assert "请修改配置" in registry.handle(",qq.admins remove c2c:u-config", admin)
    assert registry.handle(",qq.admins remove group:g1:m-owner", admin).startswith("已移除")
    assert not is_admin(config, OWNER_IN_GROUP)
    assert store.admins() == frozenset()


def test_registration_survives_restart(env, tmp_path: Path) -> None:
    registry, config, _, _ = env
    registry.handle(f",qq.claim {registry.bootstrap_code()}", OWNER_C2C)
    set_registered_admins(())

    AdminRegistry(QQPlatformStore(tmp_path / "state.json"), config, clock=Clock())

    assert is_admin(config, OWNER_C2C)


def test_claim_never_reaches_bub(tmp_path: Path, monkeypatch) -> None:
    from bub_qq.channel import QQChannel

    monkeypatch.chdir(tmp_path)
    configure.merge(
        configure._config_data,
        {"qq": {"receive_mode": "webhook", "state_file": str(tmp_path / "s.json")}},
    )
    configure._global_config.clear()
    received: list[ChannelMessage] = []
    sent: list[ChannelMessage] = []

    async def handler(message: ChannelMessage) -> None:
        received.append(message)

    async def fake_send(message: ChannelMessage) -> dict[str, object]:
        sent.append(message)
        return {"id": "sent"}

    try:
        channel = QQChannel(handler)
        channel.send_for_result = fake_send
        code = channel._admins.bootstrap_code()

        def c2c(content: str, message_id: str) -> dict:
            return {
                "id": f"e-{message_id}",
                "op": 0,
                "t": "C2C_MESSAGE_CREATE",
                "d": {
                    "author": {"user_openid": "u-owner"},
                    "content": content,
                    "id": message_id,
                    "timestamp": "2099-01-01T00:00:00+00:00",
                },
            }

        async def _run() -> None:
            await channel._handle_transport_payload(c2c(f",qq.claim {code}", "m1"))
            assert received == []
            assert sent[-1].content.startswith("已登记")
            await channel._handle_transport_payload(c2c(",qq.version", "m2"))
            assert [m.content for m in received] == [",qq.version"]
            assert received[0].kind == "command"

        asyncio.run(_run())
    finally:
        configure._global_config.clear()
        configure._config_data.clear()



def _group_code(registry: AdminRegistry) -> str:
    reply = registry.handle(",qq.claim group", OWNER_C2C)
    return reply.split("：", 1)[1].split("\n", 1)[0]


def test_group_code_is_single_use_and_short_lived(env) -> None:
    registry, config, _, _ = env
    registry.handle(f",qq.claim {registry.bootstrap_code()}", OWNER_C2C)
    assert GROUP_CODE_TTL_SECONDS == 60
    code = _group_code(registry)
    assert registry.handle(f",qq.claim {code}", OWNER_IN_GROUP).startswith("已登记")

    # Someone copying the code from the chat afterwards gets nothing.
    copier = Requester("group", "m-copier", "g1")
    assert registry.handle(f",qq.claim {code}", copier) == "配对码无效或已过期。"
    assert not is_admin(config, copier)
    assert registry.handle(f",qq.claim {code}", OWNER_IN_GROUP) == "配对码无效或已过期。"


def test_group_registration_notifies_the_issuer(env) -> None:
    registry, _, _, _ = env
    registry.handle(f",qq.claim {registry.bootstrap_code()}", OWNER_C2C)
    registry.notices.clear()
    code = _group_code(registry)
    # Someone else got to the code first: the issuer must hear about it.
    thief = Requester("group", "m-thief", "g1")
    assert registry.handle(f",qq.claim {code}", thief).startswith("已登记")
    assert len(registry.notices) == 1
    openid, text = registry.notices[0]
    assert openid == "u-owner"
    assert "group:g1:m-thief" in text
    assert ",qq.admins remove group:g1:m-thief" in text


def test_bootstrap_claim_sends_no_notice(env) -> None:
    registry, _, _, _ = env
    registry.handle(f",qq.claim {registry.bootstrap_code()}", OWNER_C2C)
    assert registry.notices == []
