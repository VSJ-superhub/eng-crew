"""Agent prompts live in .claude/skills/, not in Python string literals.

The failure mode these guard against is quiet: a renamed or mistyped skill, or a
placeholder that stops being filled, produces a prompt that still *looks* fine
and ships a literal "{task}" to the model.
"""
from __future__ import annotations

import re

import pytest

from eng_crew import prompts

# Every skill an agent asks for by name, with the values it passes.
AGENT_SKILLS = {
    "architect-decompose": {"project_context": "CTX", "raw_task": "TASK"},
    "review-patch": {"description": "D", "target_files": "F", "patch": "P"},
    "simple-edit": {"task": "T"},
    "single-agent-implement": {"task": "T", "context_block": "CB"},
    "repair-verification-failure": {"task": "T", "failures": "F"},
    "repair-verification-failure-resumed": {"failures": "F"},
}


def test_every_agent_skill_exists_on_disk():
    missing = [n for n in AGENT_SKILLS if n not in prompts.available()]
    assert not missing, f"agents reference skills with no SKILL.md: {missing}"


@pytest.mark.parametrize("name", sorted(AGENT_SKILLS))
def test_skill_has_frontmatter(name):
    skill = prompts.load(name)
    assert skill.name == name
    assert skill.description, "description is what makes a skill findable"


@pytest.mark.parametrize("name", sorted(AGENT_SKILLS))
def test_skill_body_is_not_empty(name):
    assert len(prompts.load(name).body) > 50


@pytest.mark.parametrize("name,values", sorted(AGENT_SKILLS.items()))
def test_rendering_leaves_no_unfilled_placeholder(name, values):
    rendered = prompts.render(name, **values)
    leftover = re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", rendered)
    assert not leftover, f"{name} still has placeholders: {leftover}"


@pytest.mark.parametrize("name,values", sorted(AGENT_SKILLS.items()))
def test_rendering_substitutes_every_value(name, values):
    rendered = prompts.render(name, **values)
    for key, value in values.items():
        assert str(value) in rendered, f"{name}: {key} was not substituted"


# --- substitution behaviour ---------------------------------------------


def test_json_braces_survive_substitution():
    # architect's prompt contains a JSON example; str.format would choke on it
    # and required {{ escaping. The filler must pass it through untouched.
    rendered = prompts.render("architect-decompose", project_context="c", raw_task="t")
    assert '"subtasks": [' in rendered
    assert '"id": "s1"' in rendered
    assert "{{" not in rendered


def test_unknown_placeholders_are_left_alone_not_raised():
    assert prompts.fill("keep {this} drop {that}", {"that": "X"}) == "keep {this} drop X"


def test_stray_braces_pass_through():
    assert prompts.fill('a dict: {"k": 1} and {v}', {"v": "V"}) == 'a dict: {"k": 1} and V'


def test_missing_skill_raises_a_clear_error():
    with pytest.raises(prompts.SkillNotFound):
        prompts.load("no-such-skill")


def test_skills_dir_is_overridable(tmp_path, monkeypatch):
    skill = tmp_path / "custom" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: custom\ndescription: overridden\n---\nHello {who}\n", encoding="utf-8"
    )
    monkeypatch.setenv("ENG_CREW_SKILLS_DIR", str(tmp_path))
    prompts.clear_cache()
    try:
        assert prompts.render("custom", who="world") == "Hello world"
    finally:
        prompts.clear_cache()


def test_no_agent_still_carries_an_inline_prompt():
    """Prompts belong in skill files; a new f-string prompt is a regression."""
    import pathlib

    offenders = []
    for path in pathlib.Path("eng_crew").rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8")
        if 'prompt = f"""' in text or "_PROMPT_TEMPLATE = " in text:
            offenders.append(str(path))
    assert not offenders, f"inline prompts reintroduced in: {offenders}"
