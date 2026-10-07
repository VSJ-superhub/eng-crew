"""Where an eng-crew install keeps its config and its data.

eng-crew grew up as one developer's checkout, so everything was anchored to the
package's parent directory: the `.env` next to `eng_crew/`, the tracking DB in
`.eng-crew/` beside it, logs in `logs/`. Installed from PyPI that parent
directory is `site-packages`, which is the wrong place for a database (pip
removes it on upgrade) and a hopeless place to put an API key.

These tests pin the two layouts apart. A source checkout keeps using exactly the
files it already has — nobody's working setup moves. An installed package reads
`.env` from the directory you are working in or `~/.eng-crew/.env`, and keeps its
data under `~/.eng-crew`, never inside the package.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from eng_crew import config


def make_checkout(tmp_path: Path) -> Path:
    """A source checkout as a user would have one: with a .env and a DB in it."""
    root = tmp_path / "eng-crew"
    (root / "eng_crew").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "eng-crew"\n', encoding="utf-8")
    (root / ".env").write_text("ENG_CREW_PROVIDER=claude_cli\n", encoding="utf-8")
    (root / ".eng-crew").mkdir()
    (root / ".eng-crew" / "tracking.db").write_bytes(b"")
    return root


def make_site_packages(tmp_path: Path) -> Path:
    """What `pip install eng-crew` leaves behind: a package and nothing else."""
    root = tmp_path / "site-packages"
    (root / "eng_crew").mkdir(parents=True)
    return root


# --- criterion 3: an installed eng-crew keeps nothing in site-packages -----


def test_an_installed_eng_crew_reads_env_and_stores_data_outside_the_package(
    tmp_path, monkeypatch
):
    root = make_site_packages(tmp_path)
    home = tmp_path / "home"
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("ENG_CREW_HOME", raising=False)

    assert not config.is_source_checkout(root)

    candidates = config.env_files(root=root, cwd=cwd)
    assert cwd / ".env" in candidates, "an installed eng-crew ignores the .env you run next to"
    assert home / ".eng-crew" / ".env" in candidates, "~/.eng-crew/.env is not consulted"
    assert candidates.index(cwd / ".env") > candidates.index(home / ".eng-crew" / ".env"), \
        "the .env in the working directory must win over the one in ~/.eng-crew"
    assert all(root not in c.parents for c in candidates), \
        f"an installed eng-crew still looks for .env inside the package: {candidates}"

    data = config.default_data_dir(root=root)
    assert data == home / ".eng-crew"
    assert root not in data.parents, "installed data would land in site-packages"


# --- criterion 4: a source checkout keeps exactly the files it has ---------


def test_a_source_checkout_keeps_using_its_own_env_and_tracking_db(tmp_path, monkeypatch):
    root = make_checkout(tmp_path)
    home = tmp_path / "home"
    other = tmp_path / "somewhere-else"
    other.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("ENG_CREW_HOME", raising=False)

    assert config.is_source_checkout(root)
    assert config.env_files(root=root, cwd=other) == [root / ".env"], \
        "a checkout's own .env is no longer the one that is read"
    assert config.default_data_dir(root=root) == root / ".eng-crew"
    assert (config.default_data_dir(root=root) / "tracking.db").exists(), \
        "the checkout's existing tracking.db would be abandoned"


def test_the_data_dir_can_be_pointed_anywhere_and_is_never_relative_to_the_package():
    cfg = config.Settings(data_dir="~/somewhere/else")
    assert cfg.data_dir == Path.home() / "somewhere" / "else"
    assert config._PKG_DIR not in cfg.data_dir.parents


# --- criterion 5: no author-specific paths left ---------------------------


def test_no_author_specific_paths_or_bundled_project_list_remain():
    pkg = Path(config.__file__).resolve().parent
    assert not (pkg / "bot_config.json").exists(), \
        "eng_crew/bot_config.json still ships one developer's project list"

    offenders = []
    for path in pkg.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "Users/alway" in line or "Users\alway" in line:
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], f"hardcoded author paths remain: {offenders}"


# --- criterion 6: registering a project imports nothing that is missing ---


def test_registering_a_project_raises_no_background_error(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew import tracker

    projects_mod = __import__(
        "eng_crew.dashboard.routers.projects", fromlist=["router"]
    )
    source = Path(projects_mod.__file__).read_text(encoding="utf-8")
    assert "entroly_integration" not in source, \
        "the projects router still imports the missing entroly_integration module"

    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "projects.db")
    tracker.close_connection()
    tracker._init_db()
    try:
        from eng_crew.dashboard.app import app

        proj = tmp_path / "proj"
        proj.mkdir()
        with TestClient(app) as client:
            body = client.post(
                "/api/projects", json={"name": "p", "project_path": str(proj)}
            ).json()
        assert "id" in body, body
    finally:
        tracker.close_connection()


# --- criterion 7: the docs say what the code does -------------------------


def test_the_docs_describe_where_config_lives_and_what_an_unavailable_review_does():
    root = Path(config.__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    env_example = (root / ".env.example").read_text(encoding="utf-8")

    assert "~/.eng-crew" in readme, "the README never says where an installed eng-crew keeps things"
    assert "ENG_CREW_HOME" in env_example, ".env.example does not document ENG_CREW_HOME"
    assert "unavailable" in readme.lower(), \
        "the README still describes an unreachable reviewer as a verdict"
