from __future__ import annotations

import json

import pytest

from bub_dynamic_workflows.errors import WorkflowSpecError
from bub_dynamic_workflows.graph import topological_node_ids
from bub_dynamic_workflows.spec import load_workflow_spec, load_workflow_spec_file


def test_spec_accepts_minimal_dag() -> None:
    spec = load_workflow_spec(
        {
            "name": "demo",
            "description": "Demo workflow",
            "nodes": [
                {"id": "scan", "prompt": "scan"},
                {"id": "summary", "depends_on": ["scan"], "prompt": "summary {nodes.scan}"},
            ],
        }
    )

    assert topological_node_ids(spec) == ["scan", "summary"]


def test_spec_rejects_unknown_dependency() -> None:
    with pytest.raises(WorkflowSpecError, match="unknown node"):
        load_workflow_spec(
            {
                "name": "demo",
                "description": "Demo workflow",
                "nodes": [{"id": "summary", "depends_on": ["scan"], "prompt": "summary"}],
            }
        )


def test_spec_rejects_cycle() -> None:
    with pytest.raises(WorkflowSpecError, match="cycle"):
        load_workflow_spec(
            {
                "name": "cycle",
                "description": "Cycle workflow",
                "nodes": [
                    {"id": "a", "depends_on": ["c"], "prompt": "a"},
                    {"id": "b", "depends_on": ["a"], "prompt": "b"},
                    {"id": "c", "depends_on": ["b"], "prompt": "c"},
                ],
            }
        )


def test_template_directory_loads_assets_metadata(tmp_path) -> None:
    template_dir = tmp_path / "template"
    metadata = template_dir / "assets" / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps(
            {
                "name": "template_demo",
                "description": "Template workflow",
                "nodes": [{"id": "node", "prompt": "run"}],
            }
        ),
        encoding="utf-8",
    )

    assert load_workflow_spec_file(template_dir).name == "template_demo"
