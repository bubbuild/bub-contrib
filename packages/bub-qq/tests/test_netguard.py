from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bub_qq.inbound.persist import download_to_path
from bub_qq.netguard import BLOCKED_GUIDANCE
from bub_qq.netguard import BLOCKED_MESSAGE
from bub_qq.netguard import QQ_MEDIA_DOMAINS
from bub_qq.netguard import BlockedAddressError
from bub_qq.netguard import PublicOnlyResolver
from bub_qq.netguard import check_public_url
from bub_qq.netguard import is_public_address
from bub_qq.outbound.media import download_media_url
from bub_qq.outbound.media import media_spec_from_args


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://[::1]/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://0.0.0.0/x",
        "file:///etc/passwd",
        "http:///no-host",
    ],
)
def test_check_public_url_rejects_non_public_targets(url: str) -> None:
    with pytest.raises(BlockedAddressError):
        check_public_url(url)


def test_check_public_url_allows_public_ip_and_hostnames() -> None:
    check_public_url("https://93.184.215.14/a.png")
    # Hostnames pass here; PublicOnlyResolver checks what they resolve to.
    check_public_url("https://example.com/a.png")
    assert is_public_address("8.8.8.8")
    assert not is_public_address("fe80::1%eth0")


class _FakeResolver:
    def __init__(self, hosts: list[str]) -> None:
        self._hosts = hosts
        self.closed = False

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": address,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
            for address in self._hosts
        ]

    async def close(self) -> None:
        self.closed = True


def test_public_only_resolver_filters_private_answers() -> None:
    async def _run() -> None:
        mixed = PublicOnlyResolver(_FakeResolver(["10.0.0.1", "93.184.215.14"]))
        results = await mixed.resolve("mixed.example", 443)
        assert [r["host"] for r in results] == ["93.184.215.14"]

        private = PublicOnlyResolver(_FakeResolver(["127.0.0.1", "169.254.169.254"]))
        with pytest.raises(BlockedAddressError, match=BLOCKED_MESSAGE):
            await private.resolve("rebind.example", 80)

    asyncio.run(_run())


def test_qq_media_domains_are_trusted_behind_fake_ip_proxies() -> None:
    # Surge and Clash resolve every name into 198.18.0.0/15 (not public).
    async def _run() -> None:
        trusting = PublicOnlyResolver(
            _FakeResolver(["198.18.184.114"]), trusted_domains=QQ_MEDIA_DOMAINS
        )
        for host in ("multimedia.nt.qq.com.cn", "gchat.qpic.cn", "qq.com", "QQ.COM."):
            assert [r["host"] for r in await trusting.resolve(host, 443)] == ["198.18.184.114"]
        # Only those domains and their subdomains, not look-alikes.
        for host in ("evilqq.com", "qq.com.evil.example", "example.com"):
            with pytest.raises(BlockedAddressError, match=BLOCKED_MESSAGE):
                await trusting.resolve(host, 443)
        # Other downloads (media_url, web.fetch) do not trust them.
        with pytest.raises(BlockedAddressError):
            await PublicOnlyResolver(_FakeResolver(["198.18.184.114"])).resolve("gchat.qpic.cn", 443)

    asyncio.run(_run())


def test_trusted_domains_do_not_exempt_ip_literals() -> None:
    # A redirect from a QQ host to a private IP is still refused.
    with pytest.raises(BlockedAddressError, match=BLOCKED_MESSAGE):
        check_public_url("http://10.0.0.5/x", trusted_domains=QQ_MEDIA_DOMAINS)
    check_public_url("https://multimedia.nt.qq.com.cn/download", trusted_domains=QQ_MEDIA_DOMAINS)


def _redirect_app() -> web.Application:
    async def file(request: web.Request) -> web.Response:
        return web.Response(body=b"payload")

    async def hop(request: web.Request) -> web.Response:
        raise web.HTTPFound("/file")

    async def evil(request: web.Request) -> web.Response:
        raise web.HTTPFound("http://blocked.invalid/secret")

    async def loop(request: web.Request) -> web.Response:
        raise web.HTTPFound("/loop")

    app = web.Application()
    app.router.add_get("/file", file)
    app.router.add_get("/hop", hop)
    app.router.add_get("/evil", evil)
    app.router.add_get("/loop", loop)
    return app


def _guard_blocking(host: str):
    seen: list[str] = []

    def guard(url: str) -> None:
        seen.append(url)
        if host in url:
            raise BlockedAddressError(f"blocked {url}")

    return guard, seen


def test_download_guard_checks_every_redirect_hop(tmp_path: Path) -> None:
    async def _run() -> None:
        server = TestServer(_redirect_app())
        await server.start_server()
        try:
            base = str(server.make_url(""))
            async with aiohttp.ClientSession() as session:
                guard, seen = _guard_blocking("blocked.invalid")
                dest = tmp_path / "ok.bin"
                await download_to_path(
                    session, f"{base}/hop", dest, url_guard=guard
                )
                assert dest.read_bytes() == b"payload"
                assert seen == [f"{base}/hop", f"{base}/file"]

                guard, seen = _guard_blocking("blocked.invalid")
                with pytest.raises(BlockedAddressError):
                    await download_to_path(
                        session, f"{base}/evil", tmp_path / "evil.bin", url_guard=guard
                    )
                assert seen[-1] == "http://blocked.invalid/secret"
                assert not (tmp_path / "evil.bin").exists()

                guard, _ = _guard_blocking("blocked.invalid")
                with pytest.raises(ValueError, match="redirects"):
                    await download_to_path(
                        session,
                        f"{base}/loop",
                        tmp_path / "loop.bin",
                        url_guard=guard,
                        max_redirects=3,
                    )
        finally:
            await server.close()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:9/x.png", "http://localhost:9/x.png"]
)
def test_download_media_url_refuses_loopback(tmp_path: Path, url: str) -> None:
    with pytest.raises(BlockedAddressError):
        asyncio.run(download_media_url(url, workspace=tmp_path))
    assert not (tmp_path / "outbox").exists() or not any(
        (tmp_path / "outbox").iterdir()
    )


def test_media_spec_rejects_private_literal_early() -> None:
    spec, error = media_spec_from_args("http://169.254.169.254/latest/meta-data")
    assert spec is None
    assert error == f"Not sent: {BLOCKED_MESSAGE} {BLOCKED_GUIDANCE}"


def _fetch_decision(url: str, monkeypatch, allow_hosts: str = ""):
    import bub
    from bub.hooks.interception import ToolCall

    from bub_qq import plugin
    from bub_qq.config import QQConfig

    config = QQConfig.model_construct(
        admin_users="",
        group_tool_policy="restricted",
        c2c_tool_policy="open",
        denied_tools="",
        group_shell="approval",
        c2c_access="admin_users",
        state_file="",
        download_allow_hosts=allow_hosts,
    )
    monkeypatch.setattr(bub, "ensure_config", lambda cls: config)
    state = {
        "qq": {"scope": "group", "sender_id": "m", "group_openid": "g", "session_id": "s"},
        "_runtime_workspace": "/tmp",
    }
    call = ToolCall(run_id="r", tool="web_fetch", arguments={"url": url})
    return asyncio.run(plugin.before_tool_call(call, state))


def test_web_fetch_to_loopback_is_refused_by_default(monkeypatch) -> None:
    blocked = _fetch_decision("http://127.0.0.1:9/file", monkeypatch)
    assert blocked is not None and blocked.action == "deny"
    assert blocked.message == f"web.fetch refused: {BLOCKED_MESSAGE} {BLOCKED_GUIDANCE}"


def test_web_fetch_follows_redirects_through_the_guard(monkeypatch, tmp_path) -> None:
    import threading

    ready = threading.Event()
    holder: dict = {}

    def serve() -> None:
        loop = asyncio.new_event_loop()
        server = TestServer(_redirect_app(), host="127.0.0.1")
        loop.run_until_complete(server.start_server())
        holder.update(loop=loop, server=server)
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(5)
    base = str(holder["server"].make_url(""))
    try:
        ok = _fetch_decision(f"{base}/hop", monkeypatch, allow_hosts="127.0.0.1")
        assert ok is not None and ok.action == "replace"
        assert ok.result == "payload"
        # The allowed host redirects to a host that is not allowed.
        evil = _fetch_decision(f"{base}/evil", monkeypatch, allow_hosts="127.0.0.1")
        assert evil is not None and evil.action == "deny"
    finally:
        loop = holder["loop"]
        asyncio.run_coroutine_threadsafe(holder["server"].close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)


def test_interrupted_download_leaves_no_part_file(tmp_path: Path) -> None:
    async def broken(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Length": "1000"})
        await response.prepare(request)
        await response.write(b"partial")
        # Close the connection before the promised 1000 bytes arrive.
        request.transport.close()
        return response

    app = web.Application()
    app.router.add_get("/broken", broken)

    async def _run() -> None:
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises((aiohttp.ClientError, ValueError)):
                    await download_to_path(
                        session, str(server.make_url("/broken")), tmp_path / "f.bin"
                    )
        finally:
            await server.close()

    asyncio.run(_run())
    assert list(tmp_path.iterdir()) == []
