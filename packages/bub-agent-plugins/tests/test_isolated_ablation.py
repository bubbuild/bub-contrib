from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RUN_ISOLATED_ABLATION = os.environ.get("BUB_RUN_ISOLATED_ABLATION") == "1"

pytestmark = pytest.mark.skipif(
    not RUN_ISOLATED_ABLATION,
    reason="set BUB_RUN_ISOLATED_ABLATION=1 to create and test an isolated venv",
)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    if completed.returncode:
        rendered = " ".join(command)
        pytest.fail(
            f"command failed ({completed.returncode}): {rendered}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def _bub_source(repository_root: Path) -> str:
    configured = os.environ.get("BUB_ABLATION_BUB_SOURCE")
    if configured:
        return configured
    sibling_checkout = repository_root.parent / "bub"
    if (sibling_checkout / "pyproject.toml").is_file():
        return str(sibling_checkout)
    return "bub @ git+https://github.com/bubbuild/bub.git"


def test_standard_plugin_in_an_isolated_environment(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is required for the isolated ablation check")

    package_root = Path(__file__).parents[1]
    repository_root = Path(__file__).parents[3]
    plugin_root = Path(__file__).parent / "fixtures" / "basic-agent-plugin"
    probe = Path(__file__).parent / "isolated_ablation_probe.py"
    mcp_package = repository_root / "packages" / "bub-mcp"
    isolated_python = tmp_path / "venv" / "bin" / "python"

    _run([uv, "venv", str(isolated_python.parent.parent), "--python", sys.executable])
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(isolated_python),
            _bub_source(repository_root),
            str(mcp_package),
            str(package_root),
        ]
    )

    results: list[dict[str, object]] = []
    for skills_enabled, mcp_enabled in (
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ):
        case_name = f"skills-{int(skills_enabled)}-mcp-{int(mcp_enabled)}"
        case_root = tmp_path / case_name
        environment = os.environ.copy()
        environment.update(
            {
                "BUB_HOME": str(case_root / "bub-home"),
                "HOME": str(case_root / "home"),
                "PATH": f"{isolated_python.parent}{os.pathsep}{environment['PATH']}",
            }
        )
        output = _run(
            [
                str(isolated_python),
                str(probe),
                "--plugin-fixture",
                str(plugin_root),
                "--workspace",
                str(case_root / "workspace"),
                "--skills-enabled",
                str(int(skills_enabled)),
                "--mcp-enabled",
                str(int(mcp_enabled)),
            ],
            env=environment,
        )
        results.append(json.loads(output.strip().splitlines()[-1]))

    for result in results:
        assert result["baseline_skill"] is True
        assert result["baseline_mcp"] == "basic-mcp-ok:baseline"
        assert result["plugin_skill"] is result["skills_enabled"]
        expected_mcp = "basic-mcp-ok:plugin" if result["mcp_enabled"] else None
        assert result["plugin_mcp"] == expected_mcp
