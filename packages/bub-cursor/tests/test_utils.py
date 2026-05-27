from __future__ import annotations

import sys
from types import ModuleType

from bub_cursor import utils


def test_with_bub_skills_links_agents_skill_dir(tmp_path, monkeypatch) -> None:
    skill_root = tmp_path / "installed-skills"
    skill_dir = skill_root / "example-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: example-skill\n---\n")

    skills_module = ModuleType("skills")
    skills_module.__path__ = [str(skill_root)]
    monkeypatch.setitem(sys.modules, "skills", skills_module)

    workspace = tmp_path / "workspace"
    with utils.with_bub_skills(workspace):
        agents_link = workspace / ".agents/skills/example-skill"
        assert agents_link.is_symlink()
        assert agents_link.resolve() == skill_dir
        assert not (workspace / ".cursor/skills/example-skill").exists()

    assert not (workspace / ".agents/skills/example-skill").exists()
    assert not (workspace / ".cursor/skills/example-skill").exists()
