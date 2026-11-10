from contextlib import asynccontextmanager

import httpx
from bub_ag_ui.config import AGUISettings
from bub_ag_ui.http import create_app
from bub_ag_ui.plugin import AGUIPlugin
from bub_ag_ui.tools import InterruptTool

from .conftest import collect, request


@asynccontextmanager
async def client_for(adapter, **kwargs):
    app = create_app(
        adapter.framework, adapter=adapter, manage_framework=False, **kwargs
    )
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        yield client


async def test_http_sse_and_cors_without_management_routes(setup):
    provider, make = setup
    provider.reply("你好")
    async with client_for(make(), settings=AGUISettings()) as client:
        response = await client.post(
            "/agent",
            json=request().model_dump(mode="json", by_alias=True),
            headers={"Origin": "http://localhost:3000"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert (
            response.headers["access-control-allow-origin"] == "http://localhost:3000"
        )
        assert '"type":"RUN_FINISHED"' in response.text
        assert "你好" in response.text
        assert not response.text.startswith("id:")
        for path in ("/threads/t1/history", "/threads/t1/runs/r1/events"):
            assert (await client.get(path)).status_code == 404
        assert (await client.post("/threads/t1/runs/r1/cancel")).status_code == 404


async def test_http_auth_validation_and_limits(setup):
    _, make = setup
    async with client_for(
        make(), settings=AGUISettings(token="secret", max_body_bytes=1024)
    ) as client:
        assert (await client.post("/agent", json={})).status_code == 401
        auth = {"Authorization": "Bearer secret"}
        assert (await client.post("/agent", json={}, headers=auth)).status_code == 422
        assert (
            await client.post("/agent", content=b"x" * 1025, headers=auth)
        ).status_code == 413


async def test_invalid_resume_is_an_agui_error_not_http_admission_error(setup):
    _, make = setup
    async with client_for(make()) as client:
        data = request(
            resume=[{"interruptId": "missing", "status": "resolved", "payload": True}]
        )
        response = await client.post(
            "/agent", json=data.model_dump(mode="json", by_alias=True)
        )
        assert response.status_code == 200
        assert '"type":"RUN_ERROR"' in response.text
        assert '"type":"RUN_FINISHED"' not in response.text


async def test_custom_auth_isolates_continuation_checkpoints(setup):
    provider, make = setup
    adapter = make(
        interrupts=(InterruptTool("approve", "Approval", {"type": "object"}),)
    )
    provider.reply(calls=[("a1", "approve", "{}")])
    view, _ = await collect(adapter, request(), user="alice")
    data = request(
        (await view.history())["messages"],
        run="resume",
        resume=[{"interruptId": "a1", "status": "resolved", "payload": True}],
    )

    async def authenticate(request):
        return request.headers.get("x-user", "")

    async with client_for(adapter, authenticate=authenticate) as client:
        body = data.model_dump(mode="json", by_alias=True)
        response = await client.post("/agent", json=body, headers={"x-user": "bob"})
        assert '"type":"RUN_ERROR"' in response.text
        assert (await client.post("/agent", json=body)).status_code == 401
        provider.reply("approved")
        response = await client.post("/agent", json=body, headers={"x-user": "alice"})
        assert '"type":"RUN_FINISHED"' in response.text


async def test_plugin_registers_cli_and_channel(setup):
    import typer
    from typer.testing import CliRunner

    _, make = setup
    plugin = AGUIPlugin(make().framework)
    assert plugin.provide_channels(None)[0].name == "ag-ui"
    app = typer.Typer()
    plugin.register_cli_commands(app)
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "AG-UI" in result.output
