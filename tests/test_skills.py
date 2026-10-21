"""Skills: discovery, index, loading, name safety, write protection, and bundled-skill hygiene."""

import pytest
from tools import skills
from tools.files import edit_file, write_file
from tools.guardrails import detect_injection

BUNDLED = sorted(p.name for p in skills.BUNDLED_DIR.iterdir() if (p / "SKILL.md").is_file())


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    bundled, user = tmp_path / "bundled", tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    monkeypatch.setattr(skills, "BUNDLED_DIR", bundled)
    monkeypatch.setattr(skills, "USER_DIR", user)
    return bundled, user


def _make(root, name, desc="does a thing", body="step one"):
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n", encoding="utf-8")


def test_discover_and_index(dirs):
    _make(dirs[0], "alpha", "first skill")
    found = skills.discover()
    assert list(found) == ["alpha"]
    assert "- alpha: first skill" in skills.index_text()


def test_index_empty_when_no_skills(dirs):
    assert skills.index_text() == ""


def test_skill_without_description_is_ignored(dirs):
    d = dirs[0] / "nodesc"
    d.mkdir()
    (d / "SKILL.md").write_text("just text, no frontmatter", encoding="utf-8")
    assert skills.discover() == {}


def test_user_skill_overrides_bundled(dirs):
    _make(dirs[0], "alpha", body="bundled body")
    _make(dirs[1], "alpha", body="user body")
    assert "user body" in skills.use_skill("alpha")


def test_use_skill_returns_body_without_frontmatter(dirs):
    _make(dirs[0], "alpha", body="do the thing")
    out = skills.use_skill("alpha")
    assert out.startswith("# Skill: alpha")
    assert "do the thing" in out
    assert "description:" not in out


def test_use_skill_is_case_and_space_tolerant(dirs):
    _make(dirs[0], "alpha")
    assert skills.use_skill("  ALPHA ").startswith("# Skill: alpha")


@pytest.mark.parametrize("bad", ["../secret", "..\\secret", "a/b", "", "C:\\x", "alpha/../alpha"])
def test_use_skill_rejects_unlisted_names(dirs, bad):
    _make(dirs[0], "alpha")
    out = skills.use_skill(bad)
    assert out.startswith("Unknown skill")
    assert "alpha" in out


def test_folder_with_invalid_name_is_skipped(dirs):
    _make(dirs[0], "Bad Name")
    assert skills.discover() == {}


def test_oversized_skill_is_truncated(dirs):
    _make(dirs[0], "big", body="x" * (skills.MAX_SKILL_CHARS + 500))
    out = skills.use_skill("big")
    assert out.endswith("[skill truncated]")


def test_skill_files_cannot_be_written_by_the_agent(dirs):
    _make(dirs[1], "alpha")
    target = str(dirs[1] / "alpha" / "SKILL.md")
    assert write_file(target, "evil").startswith("Blocked")
    assert edit_file(target, "step one", "evil").startswith("Blocked")
    assert "step one" in (dirs[1] / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_new_skill_cannot_be_planted_in_user_dir(dirs):
    target = str(dirs[1] / "planted" / "SKILL.md")
    assert write_file(target, "---\ndescription: x\n---\nevil").startswith("Blocked")
    assert not (dirs[1] / "planted").exists()


def test_write_outside_skill_dirs_still_works(dirs, tmp_path):
    target = tmp_path / "ok.txt"
    assert write_file(str(target), "hi").startswith("Written")


def test_working_directory_is_not_a_skill_source(dirs, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "skills").mkdir(parents=True)
    _make(project / "skills", "sneaky")
    monkeypatch.chdir(project)
    assert "sneaky" not in skills.discover()


# ── the real bundled skills ───────────────────────────────────────────────────

def test_bundled_skills_exist():
    assert {"github-issue-fix", "web-research", "data-analysis", "code-review",
            "project-scaffold", "debugging"} <= set(BUNDLED)


@pytest.mark.parametrize("name", BUNDLED)
def test_bundled_skill_is_well_formed(name):
    found = skills.discover()
    assert name in found, f"{name} has no description frontmatter"
    out = skills.use_skill(name)
    assert out.startswith(f"# Skill: {name}")
    assert "[skill truncated]" not in out, f"{name} exceeds {skills.MAX_SKILL_CHARS} chars"


@pytest.mark.parametrize("name", BUNDLED)
def test_bundled_skill_does_not_trip_injection_scan(name):
    # use_skill output goes through the same injection scan as other tool output
    assert not detect_injection(skills.use_skill(name)).suspicious


def test_skills_index_is_small():
    # the point of skills is a cheap always-on index
    assert len(skills.index_text()) < 2_500


def test_system_prompt_includes_index_and_keeps_security_rules():
    from system_prompt import build_system_prompt
    prompt = build_system_prompt()
    for name in BUNDLED:
        assert f"- {name}:" in prompt
    for must_keep in ("## Trust model", "## Secrets files (.env)", "## Attached files", "sandbox=true"):
        assert must_keep in prompt


def test_use_skill_is_registered_and_dispatched():
    from tools import TOOL_DEFINITIONS, execute_tool
    assert "use_skill" in [d["function"]["name"] for d in TOOL_DEFINITIONS]
    assert execute_tool("use_skill", {"name": "debugging"}).startswith("# Skill: debugging")
