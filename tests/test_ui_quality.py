"""The UI quality bar reaches the builder of a project with a web frontend.

Left to defaults, builders ship UI that works and looks unfinished. The bar is
attached from what is in the repo, so a backend-only project never pays for it
and a frontend project always gets it.
"""
from __future__ import annotations

import json

from eng_crew import prompts
from eng_crew.agents.single_agent import SingleAgentEngineer, has_web_frontend
from eng_crew.config import Settings


def _package(path, deps):
    path.mkdir(parents=True, exist_ok=True)
    (path / "package.json").write_text(json.dumps({"dependencies": deps}), encoding="utf-8")


def test_a_react_app_in_a_subdirectory_is_a_frontend(tmp_path):
    _package(tmp_path / "apps" / "web", {"next": "16", "react": "19"})
    assert has_web_frontend(tmp_path)


def test_a_node_backend_is_not_a_frontend(tmp_path):
    _package(tmp_path, {"express": "4"})
    (tmp_path / "server.py").write_text("print()", encoding="utf-8")
    assert not has_web_frontend(tmp_path)


def test_html_templates_count(tmp_path):
    (tmp_path / "app" / "templates").mkdir(parents=True)
    (tmp_path / "app" / "templates" / "card.html").write_text("<div/>", encoding="utf-8")
    assert has_web_frontend(tmp_path)


def test_dependencies_and_build_output_are_ignored(tmp_path):
    _package(tmp_path / "node_modules" / "react", {"react": "19"})
    (tmp_path / "htmlcov").mkdir()
    (tmp_path / "htmlcov" / "index.html").write_text("", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("", encoding="utf-8")
    assert not has_web_frontend(tmp_path)


def test_a_malformed_package_json_is_not_fatal(tmp_path):
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    assert not has_web_frontend(tmp_path)


def test_a_missing_project_has_no_frontend(tmp_path):
    assert not has_web_frontend(tmp_path / "nope")


def test_the_builder_prompt_carries_the_bar_only_for_a_frontend(tmp_path):
    agent = SingleAgentEngineer(Settings())
    backend = tmp_path / "backend"
    backend.mkdir()
    frontend = tmp_path / "frontend"
    _package(frontend, {"react": "19"})

    assert agent._ui_quality_block(str(backend)) == ""
    block = agent._ui_quality_block(str(frontend))
    assert "UI QUALITY BAR" in block

    rendered = prompts.render(
        "single-agent-implement", task="T", test_first_block="",
        ui_quality_block=block, context_block="", lessons_block="",
    )
    assert "Never show raw enum values" in rendered
    assert "{ui_quality_block}" not in rendered
