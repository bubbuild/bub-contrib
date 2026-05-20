from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from bub_extism.bridge import ExtismBridge
from bub_extism.cli import register_cli_commands
from bub_extism.config import ExtismSettings

runner = CliRunner()


class FakePlugin:
    exports: dict[str, Any] = {}

    def __init__(
        self,
        plugin_input: dict[str, Any] | bytes,
        *,
        wasi: bool = False,
        config: dict[str, str] | None = None,
    ) -> None:
        del plugin_input, wasi, config

    def __enter__(self) -> FakePlugin:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def function_exists(self, name: str) -> bool:
        return name in self.exports

    def call(self, function_name: str, data: str) -> Any:
        payload = json.loads(data)
        result = self.exports[function_name]
        if callable(result):
            return result(payload)
        return result


@pytest.fixture(autouse=True)
def fake_extism(monkeypatch: pytest.MonkeyPatch) -> None:
    FakePlugin.exports = {}
    monkeypatch.setitem(sys.modules, "extism", SimpleNamespace(Plugin=FakePlugin))


def _make_app(tmp_path: Path) -> tuple[typer.Typer, ExtismSettings]:
    settings = ExtismSettings(config_path=tmp_path / "extism.json")
    app = typer.Typer()
    register_cli_commands(app, settings, ExtismBridge())
    return app, settings


def _write_manifest(path: Path, *, wasm_path: str) -> None:
    path.write_text(
        json.dumps({"wasm": [{"path": wasm_path}], "allowed_hosts": ["example.com"]}),
        encoding="utf-8",
    )


def test_add_list_show_and_remove_plugin(tmp_path: Path) -> None:
    app, settings = _make_app(tmp_path)
    manifest_path = tmp_path / "plugin.manifest.json"
    _write_manifest(manifest_path, wasm_path="./demo.wasm")

    add_result = runner.invoke(
        app,
        [
            "extism",
            "add",
            "demo",
            str(manifest_path),
            "--hook",
            "build_prompt=build_prompt",
            "--hook",
            "run_model=run_model",
            "--wasi",
        ],
    )

    assert add_result.exit_code == 0
    assert "Added Extism plugin 'demo'." in add_result.stdout
    assert settings.read_config().model_dump(mode="json") == {
        "plugins": {
            "demo": {
                "manifest": {
                    "wasm": [{"path": "./demo.wasm"}],
                    "allowed_hosts": ["example.com"],
                },
                "hooks": {
                    "build_prompt": "build_prompt",
                    "run_model": "run_model",
                },
                "wasi": True,
            }
        }
    }

    list_result = runner.invoke(app, ["extism", "list"])
    assert list_result.exit_code == 0
    assert "demo" in list_result.stdout
    assert "build_prompt->build_prompt" in list_result.stdout
    assert "run_model->run_model" in list_result.stdout

    show_result = runner.invoke(app, ["extism", "show", "demo"])
    assert show_result.exit_code == 0
    assert '"path": "./demo.wasm"' in show_result.stdout

    remove_result = runner.invoke(app, ["extism", "remove", "demo"])
    assert remove_result.exit_code == 0
    assert "Removed Extism plugin 'demo'." in remove_result.stdout
    assert settings.read_config().model_dump(mode="json") == {"plugins": {}}


def test_plugin_defined_commands_share_extism_group(tmp_path: Path) -> None:
    app, _settings = _make_app(tmp_path)
    manifest_path = tmp_path / "plugin.manifest.json"
    _write_manifest(manifest_path, wasm_path="./cli.wasm")
    tmp_path.joinpath("extism.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "cli": {
                        "manifest": {"wasm": [{"path": "./cli.wasm"}]},
                        "hooks": {"register_cli_commands": "register_cli_commands"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    FakePlugin.exports = {
        "register_cli_commands": json.dumps(
            {
                "value": [
                    {
                        "name": "hello",
                        "function": "cli_hello",
                    }
                ]
            }
        ),
        "cli_hello": lambda request: json.dumps(
            {
                "value": {
                    "plugin": request["args"]["plugin"],
                    "command": request["args"]["command"],
                    "payload": request["args"]["payload"],
                }
            }
        ),
    }

    app, _settings = _make_app(tmp_path)
    result = runner.invoke(app, ["extism", "hello", '{"name":"Bub"}'])

    assert result.exit_code == 0
    assert '"plugin": "cli"' in result.stdout
    assert '"command": "hello"' in result.stdout
    assert '"name": "Bub"' in result.stdout


def test_plugin_defined_commands_reject_invalid_wrapper_shape(tmp_path: Path) -> None:
    manifest_path = tmp_path / "plugin.manifest.json"
    _write_manifest(manifest_path, wasm_path="./cli.wasm")
    tmp_path.joinpath("extism.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "cli": {
                        "manifest": {"wasm": [{"path": "./cli.wasm"}]},
                        "hooks": {"register_cli_commands": "register_cli_commands"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    FakePlugin.exports = {
        "register_cli_commands": json.dumps({"value": {"commands": "bad"}}),
    }

    with pytest.raises(RuntimeError, match="must return a list"):
        _make_app(tmp_path)


def test_plugin_defined_commands_reject_invalid_descriptor_shape(tmp_path: Path) -> None:
    manifest_path = tmp_path / "plugin.manifest.json"
    _write_manifest(manifest_path, wasm_path="./cli.wasm")
    tmp_path.joinpath("extism.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "cli": {
                        "manifest": {"wasm": [{"path": "./cli.wasm"}]},
                        "hooks": {"register_cli_commands": "register_cli_commands"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    FakePlugin.exports = {
        "register_cli_commands": json.dumps({"value": [{"name": "hello"}]}),
    }

    with pytest.raises(RuntimeError, match="requires name and function"):
        _make_app(tmp_path)
