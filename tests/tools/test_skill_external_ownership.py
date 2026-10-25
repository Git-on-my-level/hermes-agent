"""SCA-1131: autonomous curation respects external package ownership."""

import json

import pytest

from tools import skill_usage
from tools.skill_provenance import (
    BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin,
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", root)
    monkeypatch.setattr("agent.skill_utils.get_all_skills_dirs", lambda: [root])
    monkeypatch.setattr(skill_usage, "_prune_builtins_enabled", lambda: True)
    for name in ("managed", "local"):
        folder = root / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill.\n---\n\nOriginal lesson.\n")
        (folder / "references").mkdir()
        (folder / "references/notes.md").write_text("Original notes.\n")
    # A stale agent marker must not override the external owner's marker.
    skill_usage.save_usage({name: {"created_by": "agent"} for name in ("managed", "local")})
    return root


@pytest.mark.parametrize("marker,owner", [
    ("agentctl-managed.json", "agentctl"),
    ("other-managed.json", "other-tool"),
])
def test_curator_skips_external_owner_but_keeps_local_skills(library, marker, owner):
    from agent.curator import _render_candidate_list

    (library / "managed" / marker).write_text(json.dumps({"managed_by": owner}))
    assert "managed" not in skill_usage.list_agent_created_skill_names()
    assert "local" in skill_usage.list_agent_created_skill_names()
    assert "- managed " not in _render_candidate_list()
    assert "- local " in _render_candidate_list()
    assert not skill_usage.is_agent_created("managed")
    assert not skill_usage.is_curation_eligible("managed")
    for ok, message in (skill_usage.adopt_skill("managed"), skill_usage.archive_skill("managed")):
        assert not ok
        assert owner in message
    assert skill_usage.archive_skill("local")[0]
    assert (library / "managed" / "SKILL.md").exists()


@pytest.mark.parametrize("owner", ["agentctl", "hermes", None])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("action,args", [
    ("edit", {"content": "---\nname: managed\ndescription: Test skill.\n---\nChanged lesson.\n"}),
    ("patch", {"old_string": "Original lesson.", "new_string": "Changed lesson."}),
    ("write_file", {"file_path": "references/new.md", "file_content": "Changed notes."}),
    ("remove_file", {"file_path": "references/notes.md"}),
    ("delete", {"absorbed_into": "local"}),
])
def test_curator_mutations_respect_owner(library, owner, batch, action, args):
    from tools.skill_manager_tool import skill_manage
    from tools.skill_manager_guards import mark_background_review_skill_read

    folder = library / "managed"
    if owner:
        (folder / "agentctl-managed.json").write_text(json.dumps({"managed_by": owner}))
    before = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        for path in folder.rglob("*"):
            mark_background_review_skill_read(path)
        operation = dict(action="patch" if batch and action == "edit" else action, name="managed", **args)
        result = json.loads(skill_manage(action="", name="", operations=[operation]) if batch else skill_manage(**operation))
    finally:
        reset_current_write_origin(token)
    if owner == "agentctl":
        assert not result["success"], result
        assert "agentctl" in result["error"]
        assert {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before
    else:
        assert result["success"], result
