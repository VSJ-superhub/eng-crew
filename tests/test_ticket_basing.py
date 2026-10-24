"""A ticket's run starts from its prerequisites' work, not from behind it.

A prerequisite counts as met once it is `done`, but a done ticket's work lives
on its run's branch — and nothing merges that branch for you. So a dependent
ticket forking from the project checkout's HEAD started from code where its
prerequisite had never happened, and the agent rebuilt what was already built.

These tests pin the basing rule, against real git repositories: HEAD when the
prerequisite is already in it, the prerequisite's branch when there is one
unmerged, a branch merging them onto HEAD when there are several, and a refusal
before anything starts when they cannot be combined. The choice, and why, is
recorded on the run, and an explicit base from the caller still wins.
"""
from __future__ import annotations

import subprocess
import types

import pytest

from eng_crew import basing, dispatch, git_skill, tickets, tracker
from eng_crew.config import Settings


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def stub_launcher(module, monkeypatch, launched: list | None = None):
    """Stop a surface from starting a build, without breaking git.

    The launchers use `subprocess.Popen`, and so does `subprocess.run` — which
    is how these tests drive real repositories. So the module's own handle on
    subprocess is swapped rather than the module itself.
    """
    def popen(cmd, **kw):
        if launched is None:
            pytest.fail(f"nothing should have been launched: {cmd}")
        launched.append(list(cmd))
        return types.SimpleNamespace(pid=0)

    monkeypatch.setattr(module, "subprocess", types.SimpleNamespace(
        Popen=popen,
        DEVNULL=subprocess.DEVNULL,
        CREATE_NEW_PROCESS_GROUP=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    ))


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "basing.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path, wip_limit=3, ticket_budget_usd=15.0)


@pytest.fixture
def repo(tmp_path):
    """A real repo on `main` with one commit."""
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


# --- fixtures' worth of helpers -----------------------------------------


def finish(ticket_id: int) -> None:
    """Walk a ticket from backlog all the way to done."""
    for status in (tickets.READY, tickets.IN_PROGRESS, tickets.IN_REVIEW,
                   tickets.IN_QA, tickets.DONE):
        tickets.move(ticket_id, status, "tester")


def done_prerequisite(repo, *, title: str, files: dict[str, str]) -> tuple[int, str]:
    """A done ticket whose run left its work on its own branch.

    Returns (ticket_id, branch). The branch is committed and left unmerged —
    which is the real state of a done ticket nobody has merged yet.
    """
    ticket_id = tracker.add_backlog_item(
        title, "the details", str(repo), "", acceptance_criteria="- it works",
    )
    branch = f"ai-team/{title}"
    _git("checkout", "-q", "-b", branch, cwd=repo)
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", f"{title} work", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)

    run_id = tracker.create_run(title, str(repo), ticket_id=ticket_id)
    tracker.update_run_branch(run_id, branch)
    tracker.update_run_status(run_id, "completed")
    finish(ticket_id)
    return ticket_id, branch


def dependent(repo, *, depends_on: list[int], title: str = "dependent") -> int:
    return tracker.add_backlog_item(
        title, "the details", str(repo), "", acceptance_criteria="- it works",
        depends_on=depends_on,
    )


# --- criterion 1: a merged prerequisite needs nothing extra -------------


def test_a_prerequisite_already_in_head_leaves_the_run_on_head(db, repo):
    first, branch = done_prerequisite(repo, title="schema", files={"schema.py": "T = 1\n"})
    ticket_id = dependent(repo, depends_on=[first])
    # Unmerged, it is worth basing on; this is the state the next line changes.
    assert basing.resolve_base(ticket_id).ref == branch

    _git("merge", "-q", "--no-edit", branch, cwd=repo)      # someone merged it

    base = basing.resolve_base(ticket_id)

    assert base.ref is None                                  # fork from HEAD, as today
    assert "HEAD" in base.reason


# --- criterion 2: one unmerged prerequisite branch ----------------------


def test_one_unmerged_prerequisite_branch_becomes_the_base(db, repo):
    first, branch = done_prerequisite(repo, title="schema", files={"schema.py": "T = 1\n"})
    ticket_id = dependent(repo, depends_on=[first])

    base = basing.resolve_base(ticket_id)

    assert base.ref == branch
    assert f"#{first}" in base.reason


# --- criterion 3: several, merged onto HEAD — or refused ----------------


def test_several_unmerged_prerequisite_branches_are_merged_onto_head(db, repo):
    first, one = done_prerequisite(repo, title="schema", files={"schema.py": "T = 1\n"})
    second, two = done_prerequisite(repo, title="api", files={"api.py": "E = 1\n"})
    ticket_id = dependent(repo, depends_on=[first, second])

    base = basing.resolve_base(ticket_id)

    assert base.ref not in (None, one, two)                  # a branch of its own
    # It holds both prerequisites' work and HEAD's.
    assert git_skill.is_contained_in(repo, one, base.ref)
    assert git_skill.is_contained_in(repo, two, base.ref)
    assert git_skill.is_contained_in(repo, "main", base.ref)
    assert f"#{first}" in base.reason and f"#{second}" in base.reason
    # The main checkout is where the developer is sitting: untouched.
    assert git_skill.current_branch(repo) == "main"


def test_the_pre_flight_check_leaves_no_trial_branch_behind(db, repo):
    """A surface checks before claiming; the build makes its own base later."""
    first, _ = done_prerequisite(repo, title="schema", files={"schema.py": "T = 1\n"})
    second, _ = done_prerequisite(repo, title="api", files={"api.py": "E = 1\n"})
    ticket_id = dependent(repo, depends_on=[first, second])
    before = _git("branch", "--list", cwd=repo).stdout

    basing.ensure_buildable(ticket_id)

    assert _git("branch", "--list", cwd=repo).stdout == before


def test_conflicting_prerequisites_refuse_the_build_and_leave_the_ticket_alone(
    db, repo, cfg, monkeypatch
):
    """Discord's `/build` goes through dispatch.build_ticket. Nothing starts."""
    first, _ = done_prerequisite(repo, title="left", files={"app.py": "print('left')\n"})
    second, _ = done_prerequisite(repo, title="right", files={"app.py": "print('right')\n"})
    ticket_id = dependent(repo, depends_on=[first, second])
    stub_launcher(dispatch, monkeypatch)

    result = dispatch.build_ticket(ticket_id, actor="alice", settings=cfg)

    assert not result.ok
    assert f"#{first}" in result.detail and f"#{second}" in result.detail
    # Still in the backlog: the refusal lands before the approval to ready.
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.BACKLOG


def test_a_refused_basing_leaves_no_branch_behind(db, repo):
    first, _ = done_prerequisite(repo, title="left", files={"app.py": "print('left')\n"})
    second, _ = done_prerequisite(repo, title="right", files={"app.py": "print('right')\n"})
    ticket_id = dependent(repo, depends_on=[first, second])
    before = _git("branch", "--list", cwd=repo).stdout

    with pytest.raises(basing.ConflictingPrerequisites):
        basing.resolve_base(ticket_id)

    assert _git("branch", "--list", cwd=repo).stdout == before


def commit_on_main(repo, files: dict[str, str], message: str = "meanwhile") -> None:
    """Other work landing on HEAD after a prerequisite branched off it."""
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", message, cwd=repo)


def stacked_prerequisite(repo, *, on: str, title: str, files: dict[str, str]) -> tuple[int, str]:
    """A done ticket whose run was based on another prerequisite's branch."""
    ticket_id, branch = done_prerequisite(repo, title=title, files={})
    _git("branch", "-qD", branch, cwd=repo)
    _git("checkout", "-q", "-b", branch, on, cwd=repo)
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", f"{title} work", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    return ticket_id, branch


def test_one_prerequisite_branch_behind_head_is_merged_onto_it(db, repo):
    """Forking from the branch alone would lose what landed on HEAD since."""
    first, branch = done_prerequisite(repo, title="schema", files={"schema.py": "T = 1\n"})
    commit_on_main(repo, {"ui.py": "U = 1\n"})
    ticket_id = dependent(repo, depends_on=[first])

    base = basing.resolve_base(ticket_id)

    assert base.ref not in (None, branch)
    assert git_skill.is_contained_in(repo, branch, base.ref)
    assert git_skill.is_contained_in(repo, "main", base.ref)
    assert f"#{first}" in base.reason and "moved on" in base.reason
    assert git_skill.current_branch(repo) == "main"


def test_a_prerequisite_conflicting_with_head_is_named_as_such(db, repo):
    first, _ = done_prerequisite(repo, title="left", files={"app.py": "print('left')\n"})
    commit_on_main(repo, {"app.py": "print('main')\n"})
    ticket_id = dependent(repo, depends_on=[first])
    before = _git("branch", "--list", cwd=repo).stdout

    with pytest.raises(basing.ConflictingPrerequisites) as refused:
        basing.resolve_base(ticket_id)

    assert refused.value.with_head
    assert refused.value.conflicting == [first]
    assert "conflicts with HEAD" in str(refused.value)
    assert _git("branch", "--list", cwd=repo).stdout == before


def test_a_stacked_prerequisite_is_carried_by_the_branch_built_on_it(db, repo):
    first, one = done_prerequisite(repo, title="api", files={"api.py": "E = 1\n"})
    second, two = stacked_prerequisite(repo, on=one, title="web", files={"web.py": "W = 1\n"})
    ticket_id = dependent(repo, depends_on=[first, second])

    base = basing.resolve_base(ticket_id)

    # One branch holds both, and HEAD has not moved: no merge branch needed.
    assert base.ref == two
    assert base.prerequisites == [first, second]


def test_a_stacked_chain_behind_a_conflicting_head_blames_head_not_the_chain(db, repo):
    """The case that read as "#B conflict": B's branch clashed with HEAD alone."""
    first, one = done_prerequisite(repo, title="api", files={"api.py": "E = 1\n"})
    second, two = stacked_prerequisite(repo, on=one, title="web", files={"app.py": "print('web')\n"})
    commit_on_main(repo, {"app.py": "print('main')\n"})
    ticket_id = dependent(repo, depends_on=[second, first])

    with pytest.raises(basing.ConflictingPrerequisites) as refused:
        basing.resolve_base(ticket_id)

    assert refused.value.with_head
    assert refused.value.conflicting == [second]
    assert "conflicts with HEAD" in str(refused.value)


def test_prerequisites_that_conflict_with_each_other_say_so(db, repo):
    first, _ = done_prerequisite(repo, title="left", files={"app.py": "print('left')\n"})
    second, _ = done_prerequisite(repo, title="right", files={"app.py": "print('right')\n"})
    ticket_id = dependent(repo, depends_on=[first, second])

    with pytest.raises(basing.ConflictingPrerequisites) as refused:
        basing.resolve_base(ticket_id)

    assert not refused.value.with_head
    assert refused.value.conflicting == [first, second]
    assert "with each other" in str(refused.value)


# --- criterion 4: recorded on the run, and shown -----------------------


def test_the_run_records_the_branch_it_was_based_on_and_why(db, repo, cfg, monkeypatch):
    from eng_crew import run as run_mod

    first, branch = done_prerequisite(repo, title="schema", files={"schema.py": "T = 1\n"})
    ticket_id = dependent(repo, depends_on=[first])
    tickets.move(ticket_id, tickets.READY, "tester")

    seen: dict = {}

    def fake_pipeline(**kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(run_mod, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(run_mod, "_review_ticket", lambda *a, **kw: None)

    run_mod.run_ticket(ticket_id, actor="cli", settings=cfg)

    assert seen["base_branch"] == branch
    run = tracker.get_ticket_run(ticket_id)
    assert run["base_branch"] == branch
    assert f"#{first}" in run["base_reason"]


def test_the_dashboard_shows_what_a_run_was_based_on(db, repo):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    run_id = tracker.create_run("a task", str(repo))
    tracker.update_run_base(run_id, "ai-team/schema", "#7's branch is not in HEAD yet")

    with TestClient(app) as client:
        body = client.get(f"/api/run/{run_id}").json()

    assert body["run"]["base_branch"] == "ai-team/schema"
    assert "#7" in body["run"]["base_reason"]


# --- criterion 5: every surface, and an explicit base wins -------------


def test_an_explicit_base_from_the_caller_wins(db, repo, cfg, monkeypatch):
    from eng_crew import run as run_mod

    first, prereq_branch = done_prerequisite(
        repo, title="schema", files={"schema.py": "T = 1\n"}
    )
    _git("branch", "mine", cwd=repo)
    ticket_id = dependent(repo, depends_on=[first])
    tickets.move(ticket_id, tickets.READY, "tester")

    # Asked for directly, the resolver does not go looking at prerequisites.
    assert basing.resolve_base(ticket_id, explicit_base="mine").ref == "mine"

    seen: dict = {}
    monkeypatch.setattr(run_mod, "run_pipeline", lambda **kw: seen.update(kw) or {})
    monkeypatch.setattr(run_mod, "_review_ticket", lambda *a, **kw: None)

    run_mod.run_ticket(ticket_id, actor="cli", base_branch="mine", settings=cfg)

    assert seen["base_branch"] == "mine"
    assert seen["base_branch"] != prereq_branch
    assert tracker.get_ticket_run(ticket_id)["base_branch"] == "mine"


def test_every_surface_that_starts_a_build_resolves_the_base(db, repo, cfg, monkeypatch):
    """The CLI, the dashboard, Discord and the queue all end in `ticket <id>`.

    Each of them launches that one command, which resolves the base itself — so
    none of them can start a build on the wrong code, and none of them pins a
    base the resolver was not asked for.
    """
    from eng_crew.dashboard.routers import runs as runs_router

    first, _ = done_prerequisite(repo, title="schema", files={"schema.py": "T = 1\n"})

    launched: list[list[str]] = []

    # Discord's /build and the MCP dispatch.
    discord_ticket = dependent(repo, depends_on=[first], title="from discord")
    dash_ticket = dependent(repo, depends_on=[first], title="from the dashboard")
    tickets.move(dash_ticket, tickets.READY, "tester")
    stub_launcher(dispatch, monkeypatch, launched)
    stub_launcher(runs_router, monkeypatch, launched)
    monkeypatch.setattr(
        runs_router, "get_active_runs_for_project", lambda path: [], raising=False
    )

    assert dispatch.build_ticket(discord_ticket, actor="alice", settings=cfg).started

    # The dashboard's Build button, and the queue pull, which calls the same thing.
    ok, _detail = runs_router.launch_ticket_run(dash_ticket, actor="queue")
    assert ok

    assert len(launched) == 2
    for cmd in launched:
        assert "ticket" in cmd                       # `eng-crew ticket <id>`
        assert "--base" not in cmd                   # nobody pre-empts the resolver


def test_the_dashboard_refuses_a_ticket_whose_prerequisites_conflict(db, repo, monkeypatch):
    from eng_crew.dashboard.routers import runs as runs_router

    first, _ = done_prerequisite(repo, title="left", files={"app.py": "print('left')\n"})
    second, _ = done_prerequisite(repo, title="right", files={"app.py": "print('right')\n"})
    ticket_id = dependent(repo, depends_on=[first, second])
    tickets.move(ticket_id, tickets.READY, "tester")
    stub_launcher(runs_router, monkeypatch)

    ok, detail = runs_router.launch_ticket_run(ticket_id, actor="queue")

    assert not ok
    assert f"#{first}" in detail and f"#{second}" in detail
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.READY


def test_a_ticket_with_no_prerequisites_is_unchanged(db, repo):
    """The ordinary case: nothing to wait on, so nothing to base on."""
    ticket_id = dependent(repo, depends_on=[])

    base = basing.resolve_base(ticket_id)

    assert base.ref is None
    assert base.reason
