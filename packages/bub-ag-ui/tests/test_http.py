from contextlib import asynccontextmanager

import httpx
from bub_ag_ui.config import AGUISettings
from bub_ag_ui.http import create_app
from bub_ag_ui.plugin import AGUIPlugin

from .conftest import request


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


async def test_http_sse_history_replay_and_cors(setup):
    provider, make = setup
    provider.reply("你好")
    adapter = make()
    async with client_for(adapter, settings=AGUISettings()) as client:
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
        history = (await client.get("/threads/t1/history")).json()
        assert history["messages"][-1]["content"] == "你好"
        replay = await client.get("/threads/t1/runs/r1/events")
        assert replay.text == response.text
        cursor = int(response.text.split("id: ")[-1].splitlines()[0])
        tail = await client.get(
            "/threads/t1/runs/r1/events", headers={"Last-Event-ID": str(cursor)}
        )
        assert tail.text == ""
        assert len(provider.requests) == 1


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
        assert (
            await client.get("/threads/t/runs/missing/events", headers=auth)
        ).status_code == 404


async def test_custom_auth_scopes_history(setup):
    provider, make = setup
    provider.reply("alice response")

    async def authenticate(request):
        return request.headers.get("x-user", "")

    async with client_for(make(), authenticate=authenticate) as client:
        body = request().model_dump(mode="json", by_alias=True)
        assert (
            await client.post("/agent", json=body, headers={"x-user": "alice"})
        ).status_code == 200
        assert (
            await client.get("/threads/t1/history", headers={"x-user": "bob"})
        ).json()["messages"] == []
        assert (await client.get("/threads/t1/history")).status_code == 401


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
