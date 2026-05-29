from __future__ import annotations

from pathlib import Path


def test_workflow_skill_guides_lifecycle_usage() -> None:
    skill = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("src", "skills", "workflow", "SKILL.md")
        .read_text(encoding="utf-8")
    )

    assert "workflow.start" in skill
    assert "workflow.resume" in skill
    assert "workflow.validate" not in skill
    assert "Redun-visible task" in skill
    assert "status projection" in skill
    assert "Planning is normal workflow behavior" in skill
    assert "workflow." + "plan" not in skill
