from __future__ import annotations

from pathlib import Path


def test_workflow_skill_documents_tapexbee_boundaries() -> None:
    skill = (
        Path(__file__)
        .parents[1]
        .joinpath("src", "skills", "workflow", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    assert "name: workflow" in skill
    assert "tapexbee" in skill
    assert "bee topic" in skill
    assert "DAG anchors" in skill
    assert "human-reviewable milestones" in skill
    assert "Bub subagent" in skill
    assert "Bub tape store" in skill
    assert "task projection" in skill
    assert "workflow.start" in skill
    assert "workflow.step" in skill
    assert "temporary template" in skill
    assert "reusable template" in skill
    assert "input_schema" in skill
    assert "{inputs.name}" in skill
    assert "BUB_WORKFLOW_" in skill
    assert "template_dirs" in skill
    assert "{nodes.node_id.field}" in skill
    assert "function" in skill
    assert "template_ref" not in skill
    assert "workflow.run" not in skill
    assert "{args.name}" not in skill
    assert "{agents.label.field}" not in skill
    assert not any("\u4e00" <= char <= "\u9fff" for char in skill)
