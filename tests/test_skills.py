from pathlib import Path

import pytest

from taskboy import skills


def write_skill(root: Path, name: str, body: str, requires: list[str] | None = None) -> None:
    path = root / name
    path.mkdir()
    dependency_line = f"requires: {requires}\n" if requires else ""
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} description\n{dependency_line}---\n\n{body}\n")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/review", ("review", "")),
        ("/review https://example.test/pr/1", ("review", "https://example.test/pr/1")),
        ("/review first\nsecond", ("review", "first\nsecond")),
        ("review", None),
        ("/", None),
    ],
)
def test_parse_invocation(text, expected):
    assert skills.parse_invocation(text) == expected


def test_load_and_available(tmp_path):
    write_skill(tmp_path, "zeta", "zeta body")
    write_skill(tmp_path, "alpha", "alpha body", ["zeta"])
    assert skills.available(tmp_path) == ["alpha", "zeta"]
    loaded = skills.load(tmp_path, "alpha")
    assert loaded.name == "alpha"
    assert loaded.description == "alpha description"
    assert loaded.requires == ["zeta"]
    assert loaded.body == "alpha body"


def test_render_inlines_transitive_dependencies_once_breadth_first(tmp_path):
    write_skill(tmp_path, "main", "main body", ["left", "right"])
    write_skill(tmp_path, "left", "left body", ["shared"])
    write_skill(tmp_path, "right", "right body", ["shared"])
    write_skill(tmp_path, "shared", "shared body")
    rendered = skills.render(tmp_path, "main")
    assert rendered.startswith("main body")
    assert rendered.index("Included skill: /left") < rendered.index("Included skill: /right") < rendered.index("Included skill: /shared")
    assert rendered.count("Included skill: /shared") == 1


def test_missing_skill_raises(tmp_path):
    with pytest.raises(skills.SkillError, match="not installed"):
        skills.load(tmp_path, "missing")


SAMPLE_TEMPLATE_VARIABLES = {
    "agent_name": "Scout",
    "reviewer_name": "Critic",
    "github_org": "example-org",
    "repo_list": "`svc-a`, `svc-b`",
    "self_repo": "example-org/taskboy",
    "pr_target_branch": "main",
    "jira_project": "ENG",
    "jira_site": "example.atlassian.net",
    "conventions_file": "config/conventions.md",
}


def instantiate_all_templates(root: Path) -> list[str]:
    templates = Path(__file__).parents[1] / "taskboy" / "templates" / "skills"
    names = sorted(child.name for child in templates.iterdir() if child.is_dir())
    for name in names:
        text = (templates / name / "SKILL.md").read_text()
        for key, value in SAMPLE_TEMPLATE_VARIABLES.items():
            text = text.replace("{{" + key + "}}", value)
        (root / name).mkdir()
        (root / name / "SKILL.md").write_text(text)
    return names


def test_shipped_skills_dir_is_empty():
    assert skills.available(Path(__file__).parents[1] / "skills") == []


def test_all_skill_templates_instantiate_and_load(tmp_path):
    names = instantiate_all_templates(tmp_path)
    assert names
    for name in names:
        text = (tmp_path / name / "SKILL.md").read_text()
        assert "{{" not in text, f"unreplaced template variable in {name}"
        loaded = skills.load(tmp_path, name)
        assert loaded.name == name and loaded.body
        assert skills.render(tmp_path, name)  # transitive requires resolve against the same root


def test_frontmatter_model_profile_and_internal_tools_parse(tmp_path):
    (tmp_path / "disc").mkdir()
    (tmp_path / "disc" / "SKILL.md").write_text("---\nname: disc\ndescription: d\nmodel: fable\nprofile: standard\ninternal_tools: [issues, enqueue]\n---\n\nbody\n")
    loaded = skills.load(tmp_path, "disc")
    assert loaded.model == "fable" and loaded.profile == "standard" and loaded.internal_tools == ["issues", "enqueue"]


def test_defaults_when_frontmatter_omits_optional_fields(tmp_path):
    write_skill(tmp_path, "plain", "body")
    loaded = skills.load(tmp_path, "plain")
    assert loaded.model is None and loaded.profile is None and loaded.internal_tools == []


def test_unknown_internal_tool_rejected(tmp_path):
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "SKILL.md").write_text("---\nname: bad\ndescription: d\ninternal_tools: [nope]\n---\n\nbody\n")
    with pytest.raises(skills.SkillError, match="internal_tools"):
        skills.load(tmp_path, "bad")


# -- built-in skills: packaged fallback with operator override ----------------


def test_builtin_resolves_from_packaged_template_when_not_installed(tmp_path):
    loaded = skills.resolve(tmp_path, "review", {"agent_name": "Scout", "conventions_file": "CONVENTIONS.md", "self_repo": ""})
    assert loaded is not None and loaded.name == "review"
    assert "Scout" in loaded.body
    assert "{{" not in loaded.body and "{{" not in loaded.description


def test_installed_skill_overrides_the_builtin(tmp_path):
    write_skill(tmp_path, "review", "my custom review procedure")
    loaded = skills.resolve(tmp_path, "review", {"agent_name": "Scout"})
    assert loaded is not None and loaded.body == "my custom review procedure"


def test_resolve_returns_none_for_unknown_non_builtin(tmp_path):
    assert skills.resolve(tmp_path, "slack2pr") is None
    assert skills.resolve(tmp_path, "nope") is None


@pytest.mark.parametrize("name", skills.BUILTIN_SKILLS)
def test_every_builtin_renders_cleanly_with_runtime_variables(tmp_path, name):
    from tests.conftest import make_config

    variables = skills.runtime_variables(make_config())
    body = skills.render(tmp_path, name, variables)
    assert body
    assert "{{" not in body, f"builtin /{name} has a template variable runtime_variables doesn't provide"


def test_render_resolves_requires_through_builtins(tmp_path):
    (tmp_path / "watch").mkdir()
    (tmp_path / "watch" / "SKILL.md").write_text("---\nname: watch\ndescription: d\nrequires: [review]\n---\n\nwatch body\n")
    rendered = skills.render(tmp_path, "watch", {"agent_name": "Scout", "conventions_file": "CONVENTIONS.md", "self_repo": ""})
    assert "watch body" in rendered
    assert "### Included skill: /review" in rendered


def test_runtime_variables_derive_from_config():
    from tests.conftest import make_config

    variables = skills.runtime_variables(make_config())
    assert variables["agent_name"] == "Agent"
    assert variables["self_repo"] == "example-org/taskboy"
    assert variables["conventions_file"] == "CONVENTIONS.md"  # unset conventions falls back to the injected filename

    disabled_github = make_config(services={"github": False, "slack": True, "jira": True, "confluence": True, "sentry": True, "aws": True})
    assert skills.runtime_variables(disabled_github)["self_repo"] == ""  # a disabled service leaks nothing into prompts
