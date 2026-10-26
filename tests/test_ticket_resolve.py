"""A build refused for prerequisite conflicts can be unblocked without a terminal.

#100760 was refused because #100753's branch conflicted with HEAD, and the
refusal said "merge it into HEAD yourself" — which nothing on the dashboard
could do. `eng_crew.resolve` hands the stopped merge to an agent on a branch of
its own, and the next build forks from it.

These run against real git repositories; only the agent is a stand-in. It
resolves a conflict by writing what the test tells it to, the way the real one
would edit the file and `git add` it.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from eng_crew import basing, budget, git_skill, resolve, tickets, tracker
from eng_crew import providers
from eng_crew import verify as verify_mod
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "resolve.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path, wip_limit=3, ticket_budget_usd=15.0,
                    worktree_dir=str(tmp_path / "worktrees"))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "app.py").write_text("import a\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


@pytest.fixture
def agent(monkeypatch):
    """The resolving agent. ``box["write"]`` is what it leaves in each file."""
    box = {"write": {}, "calls": [], "cost": 0.42}

    def call_llm(provider, model, prompt, **kw):
        box["calls"].append({"prompt": prompt, **kw})
        for name, text in box["write"].items():
            (__import__("pathlib").Path(kw["cwd"]) / name).write_text(text, encoding="utf-8")
            _git("add", name, cwd=kw["cwd"])
        return LLMResult(text="kept both imports", cost_usd=box["cost"])

    monkeypatch.setattr(providers, "call_llm", call_llm)
    return box


def commit_on_main(repo, files):
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "main moved on", cwd=repo)


def done_prerequisite(repo, *, title, files):
    ticket_id = tracker.add_backlog_item(title, "d", str(repo), "", acceptance_criteria="- it works")
    branch = f"ai-team/{title}"
    _git("checkout", "-q", "-b", branch, cwd=repo)
    commit_on_main(repo, files)
    _git("checkout", "-q", "main", cwd=repo)
    run_id = tracker.create_run(title, str(repo), ticket_id=ticket_id)
    tracker.update_run_branch(run_id, branch)
    tracker.update_run_status(run_id, "completed")
    for status in (tickets.READY, tickets.IN_PROGRESS, tickets.IN_REVIEW, tickets.IN_QA, tickets.DONE):
        tickets.move(ticket_id, status, "tester")
    return ticket_id, branch


def conflicted_dependent(repo, **kw):
    """#100760's shape: the prerequisite and main both changed the import line."""
    first, branch = done_prerequisite(repo, title="messages", files={"app.py": "import a, messages\n"})
    commit_on_main(repo, {"app.py": "import a, profiles\n"})
    ticket_id = tracker.add_backlog_item("realtime", "d", str(repo), "",
                                         acceptance_criteria="- it works",
                                         depends_on=[first], **kw)
    tickets.move(ticket_id, tickets.READY, "tester")
    return ticket_id, first, branch


def show(repo, ref, path):
    return _git("show", f"{ref}:{path}", cwd=repo).stdout


# --- the refusal points somewhere ---------------------------------------


def test_the_refusal_names_the_way_out(db, repo):
    ticket_id, _, _ = conflicted_dependent(repo)

    with pytest.raises(basing.ConflictingPrerequisites) as refused:
        basing.resolve_base(ticket_id)

    assert basing.REFUSED in str(refused.value)
    assert f"eng-crew resolve {ticket_id}" in str(refused.value)
    assert "yourself" not in str(refused.value)


# --- resolving --------------------------------------------------------------


def test_an_agent_resolves_the_conflict_and_the_build_forks_from_it(db, repo, cfg, agent):
    ticket_id, first, branch = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}

    outcome = resolve.resolve(ticket_id, cfg)

    resolved = basing.resolved_branch(ticket_id)
    assert outcome.branch == resolved
    assert show(repo, resolved, "app.py") == "import a, messages, profiles\n"
    assert git_skill.is_contained_in(repo, "main", resolved)
    assert git_skill.is_contained_in(repo, branch, resolved)
    # The developer's checkout never moved, and nothing was left lying about.
    assert git_skill.current_branch(repo) == "main"
    assert show(repo, "main", "app.py") == "import a, profiles\n"
    assert [w for w in git_skill.list_worktrees(repo) if w.get("branch") == resolved] == []
    # The agent was told what it was merging and where it stopped.
    assert branch in agent["calls"][0]["prompt"] and "app.py" in agent["calls"][0]["prompt"]

    base = basing.resolve_base(ticket_id)
    assert base.ref == resolved
    assert f"#{first}" in base.reason


def test_the_resolution_is_charged_to_the_ticket_and_recorded(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}

    outcome = resolve.resolve(ticket_id, cfg)

    assert tracker.ticket_spend(ticket_id) == pytest.approx(0.42)
    run = tracker.get_run_detail(outcome.run_id)
    assert run["status"] == "completed"
    assert run["ticket_id"] == ticket_id
    # Its branch is the base, not its work: a dependent of this ticket must not
    # mistake the resolution for what this ticket built.
    assert not run.get("git_branch")
    assert run["base_branch"] == basing.resolved_branch(ticket_id)
    assert resolve.latest_note(ticket_id).startswith("resolve: resolved")
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.READY   # no build started
    # The session was capped at what the ticket had left.
    assert agent["calls"][0]["max_budget_usd"] == pytest.approx(15.0)


def test_a_resolution_still_holding_markers_is_thrown_away(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "<<<<<<< HEAD\nimport a, profiles\n=======\nimport a, messages\n>>>>>>> x\n"}

    with pytest.raises(resolve.ResolveFailed, match="left conflicts in app.py"):
        resolve.resolve(ticket_id, cfg)

    assert not git_skill.branch_exists(repo, basing.resolved_branch(ticket_id))
    assert tracker.get_ticket_run(ticket_id)["status"] == "failed"
    assert resolve.latest_note(ticket_id).startswith("resolve: could not resolve")
    with pytest.raises(basing.ConflictingPrerequisites):
        basing.resolve_base(ticket_id)


def test_a_resolution_that_breaks_the_build_is_thrown_away(db, repo, cfg, agent, monkeypatch):
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}
    monkeypatch.setattr(verify_mod, "verify", lambda path, **kw: types.SimpleNamespace(
        passed=False, summary=lambda: "pytest: 2 failed"))

    with pytest.raises(resolve.ResolveFailed, match="2 failed"):
        resolve.resolve(ticket_id, cfg)

    assert not git_skill.branch_exists(repo, basing.resolved_branch(ticket_id))


def test_nothing_to_resolve_costs_nothing(db, repo, cfg, agent):
    first, _ = done_prerequisite(repo, title="api", files={"api.py": "E = 1\n"})
    ticket_id = tracker.add_backlog_item("web", "d", str(repo), "", acceptance_criteria="- x",
                                         depends_on=[first])

    outcome = resolve.resolve(ticket_id, cfg)

    assert outcome.branch is None
    assert agent["calls"] == []
    assert tracker.get_ticket_runs(ticket_id) == []


def test_a_spent_budget_stops_the_resolution_before_the_agent(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo, budget_usd=1.0)
    spent = tracker.create_run("earlier", str(repo), ticket_id=ticket_id)
    tracker.log_event(spent, 0, "single_agent", LLMResult(text="", cost_usd=2.0))
    tracker.finish_run(spent, status="failed")

    with pytest.raises(budget.BudgetExhausted):
        resolve.resolve(ticket_id, cfg)

    assert agent["calls"] == []


def test_a_ticket_already_built_is_not_resolved(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo)
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")

    with pytest.raises(resolve.ResolveRefused):
        resolve.resolve(ticket_id, cfg)
    assert agent["calls"] == []


def test_a_head_that_moves_on_after_the_resolution_is_merged_onto_it(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}
    resolve.resolve(ticket_id, cfg)
    commit_on_main(repo, {"readme.md": "later\n"})

    base = basing.resolve_base(ticket_id)

    assert base.ref.startswith(basing.BASE_PREFIX + "/")
    assert "resolved" in base.reason
    assert show(repo, base.ref, "app.py") == "import a, messages, profiles\n"
    assert show(repo, base.ref, "readme.md") == "later\n"


def test_resolving_again_replaces_a_stale_resolution(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}
    resolve.resolve(ticket_id, cfg)
    # HEAD changes the same line again, so the old resolution no longer merges.
    commit_on_main(repo, {"app.py": "import a, profiles, search\n"})
    with pytest.raises(basing.ConflictingPrerequisites):
        basing.resolve_base(ticket_id)

    agent["write"] = {"app.py": "import a, messages, profiles, search\n"}
    resolve.resolve(ticket_id, cfg)

    assert basing.resolve_base(ticket_id).ref == basing.resolved_branch(ticket_id)
    assert show(repo, basing.resolved_branch(ticket_id), "app.py") == \
        "import a, messages, profiles, search\n"


# --- the dashboard ----------------------------------------------------------


def test_the_dashboard_offers_and_runs_the_resolution(db, repo, cfg, agent, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app
    from eng_crew.dashboard.routers import projects

    class Inline:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(projects, "threading", types.SimpleNamespace(Thread=Inline))
    monkeypatch.setattr(resolve, "_settings", lambda settings: cfg)
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}
    project_id = tracker.ensure_project(str(repo))

    with TestClient(app) as client:
        refused = client.post(f"/api/tickets/{ticket_id}/run")
        started = client.post(f"/api/tickets/{ticket_id}/resolve")
        queue = client.get(f"/api/projects/{project_id}/queue").json()

    assert refused.status_code == 409 and refused.json()["conflict"] is True
    assert started.json()["started"] is True
    card = next(t for t in queue["tickets"] if t["id"] == ticket_id)
    assert card["resolving"] is False
    assert card["resolution"].startswith("resolve: resolved")
    assert basing.resolve_base(ticket_id).ref == basing.resolved_branch(ticket_id)


def test_the_dashboard_refuses_to_resolve_a_ticket_past_building(db, repo):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    ticket_id, _, _ = conflicted_dependent(repo)
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")

    with TestClient(app) as client:
        response = client.post(f"/api/tickets/{ticket_id}/resolve")

    assert response.status_code == 409
