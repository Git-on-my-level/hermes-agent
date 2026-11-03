"""Disk metadata is not the injected catalog (SCA-1040 efficiency audit)."""

import json

import pytest

from agent import prompt_builder as pb


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)
    yield tmp_path
    pb.clear_skills_system_prompt_cache(clear_snapshot=True)


def test_disabled_catalog_stays_out_of_prompt_across_cache_and_config_changes(catalog):
    names = [f"audit-skill-{i:03}" for i in range(176)]
    disabled = names[:107]
    for name in names:
        category = "disabled-only" if name in disabled else "enabled"
        skill = catalog / "skills" / category / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: {name}\ndescription: Instructions for {name}.\n---\nFixture body.\n",
            encoding="utf-8",
        )
    description = catalog / "skills" / "disabled-only" / "DESCRIPTION.md"
    description.write_text("---\ndescription: Disabled category blurb.\n---\n", encoding="utf-8")

    before = pb.build_skills_system_prompt()
    snapshot = catalog / ".skills_prompt_snapshot.json"
    snapshot_bytes = snapshot.stat().st_size
    (catalog / "config.yaml").write_text(
        json.dumps({"skills": {"disabled": disabled}}), encoding="utf-8",
    )
    after = pb.build_skills_system_prompt()
    for name in disabled:
        assert name not in after
    for name in names[107:]:
        assert name in after
    assert "Disabled category blurb" not in after
    assert len(after.encode("utf-8")) < len(before.encode("utf-8"))
    assert pb.build_skills_system_prompt() == after
    pb.clear_skills_system_prompt_cache()
    assert pb.build_skills_system_prompt() == after

    # Re-enabling must recover descriptions even after a cold disk-cache load.
    (catalog / "config.yaml").write_text("skills: {}\n", encoding="utf-8")
    pb.clear_skills_system_prompt_cache()
    assert pb.build_skills_system_prompt() == before
    print(json.dumps({
        "fixture_skills": len(names), "disabled_skills": len(disabled),
        "snapshot_bytes": snapshot_bytes,
        "prompt_bytes_before_disabling": len(before.encode("utf-8")),
        "prompt_bytes_after_disabling": len(after.encode("utf-8")),
    }))


def test_platform_disabled_skills_are_neither_offered_nor_loadable(catalog, monkeypatch):
    from tools import skills_tool
    from tools.registry import registry

    skill = catalog / "skills" / "audit-platform-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: audit-platform-skill\ndescription: Platform fixture.\n---\nFixture body.\n",
        encoding="utf-8",
    )
    (catalog / "config.yaml").write_text(json.dumps({"skills": {
        "platform_disabled": {"telegram": ["audit-platform-skill"]},
    }}), encoding="utf-8")
    monkeypatch.setenv("HERMES_PLATFORM", "cli")
    assert "audit-platform-skill" in pb.build_skills_system_prompt()
    monkeypatch.setenv("HERMES_PLATFORM", "telegram")
    assert "audit-platform-skill" not in pb.build_skills_system_prompt()
    pb.clear_skills_system_prompt_cache()
    assert "audit-platform-skill" not in pb.build_skills_system_prompt()
    assert "audit-platform-skill" not in registry.dispatch("skills_list", {})
    result = json.loads(registry.dispatch("skill_view", {"name": "audit-platform-skill"}))
    assert "disabled" in result["error"]
    assert any(s["name"] == "audit-platform-skill"
               for s in skills_tool.find_all_skills(skip_disabled=True))
