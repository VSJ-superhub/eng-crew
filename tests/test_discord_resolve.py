"""A merge conflict can be heard about and fixed from a phone.

`eng_crew.resolve` could already unblock a build refused for prerequisite
conflicts, but only from a terminal or a dashboard: the refusal itself was
silent, so the ticket sat in the queue until somebody went looking. These tests
pin the two halves that close that loop — the refusal pushes through the same
webhook #100802 uses, naming the `/resolve <id>` that fixes it, and `/resolve`
runs exactly the resolution `eng-crew resolve` runs, credited to whoever asked
and reporting its outcome back.

They run against real git repositories; only the agent and the webhook are
stand-ins.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from eng_crew import basing, notify, providers, resolve, tickets, tracker
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult

discord_bot = pytest.importorskip(
    "eng_crew.discord_bot", reason="discord.py not installed (optional [discord] extra)"
)

WEBHOOK = "https://discord.com/api/webhooks/123456789/s3cret-t0ken-nobody-may-log"
USER_ID = "987654321"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


# -- fixtures -----------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "discord_resolve.db")
    tracker.close_connection()
    tracker._init_db()
    notify.forget_posted()
    yield tmp_path
    tracker.close_connection()
    notify.forget_posted()


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path, wip_limit=3, ticket_budget_usd=15.0,
                    worktree_dir=str(tmp_path / "worktrees"))


@pytest.fixture
def posts(monkeypatch):
    """Every webhook POST this test made: url, payload and timeout."""
    sent: list[dict] = []

    def fake_post(url, *, json=None, timeout=None, **kw):
        sent.append({"url": url, "payload": json, "timeout": timeout})

        class Reply:
            status_code = 204
            text = ""
        return Reply()

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    return sent


@pytest.fixture
def webhook(monkeypatch):
    from eng_crew import config

    monkeypatch.setattr(config.settings, "discord_webhook_url", WEBHOOK)
    monkeypatch.setattr(config.settings, "discord_notify_user_id", "")
    return config.settings


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
            (Path(kw["cwd"]) / name).write_text(text, encoding="utf-8")
            _git("add", name, cwd=kw["cwd"])
        return LLMResult(text="kept both imports", cost_usd=box["cost"])

    monkeypatch.setattr(providers, "call_llm", call_llm)
    return box


# -- helpers ------------------------------------------------------------------

def commit_on_main(repo, files):
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "main moved on", cwd=repo)


def done_prerequisite(repo, *, title, files):
    ticket_id = tracker.add_backlog_item(title, "d", str(repo), "",
                                         acceptance_criteria="- it works")
    branch = f"ai-team/{title}"
    _git("checkout", "-q", "-b", branch, cwd=repo)
    commit_on_main(repo, files)
    _git("checkout", "-q", "main", cwd=repo)
    run_id = tracker.create_run(title, str(repo), ticket_id=ticket_id)
    tracker.update_run_branch(run_id, branch)
    tracker.update_run_status(run_id, "completed")
    for status in (tickets.READY, tickets.IN_PROGRESS, tickets.IN_REVIEW,
                   tickets.IN_QA, tickets.DONE):
        tickets.move(ticket_id, status, "tester")
    return ticket_id, branch


def conflicted_dependent(repo, *, title="realtime", **kw):
    """#100760's shape: the prerequisite and main both changed the import line."""
    first, branch = done_prerequisite(repo, title="messages",
                                      files={"app.py": "import a, messages\n"})
    commit_on_main(repo, {"app.py": "import a, profiles\n"})
    ticket_id = tracker.add_backlog_item(title, "d", str(repo), "",
                                         acceptance_criteria="- it works",
                                         depends_on=[first], project_id=1, **kw)
    tickets.move(ticket_id, tickets.READY, "tester")
    return ticket_id, first, branch


def contents(sent: list[dict]) -> list[str]:
    return [p["payload"]["content"] for p in sent]


def history(ticket_id: int) -> list[dict]:
    return tickets.events(ticket_id)


# -- criterion 1: a refused build posts, with the command that fixes it -------

def test_a_build_refused_for_conflicts_posts_the_resolve_command(
        db, repo, cfg, webhook, posts):
    from eng_crew import dispatch

    ticket_id, first, _ = conflicted_dependent(repo)
    posts.clear()                      # the prerequisite's own "done" push

    result = dispatch.build_ticket(ticket_id, actor="tester", settings=cfg)

    assert not result.ok
    assert len(posts) == 1, contents(posts)
    message = contents(posts)[0]
    assert f"#{ticket_id}" in message                      # the ticket
    assert f"#{first}" in message                          # the prerequisite
    assert "HEAD" in message                               # what it conflicts with
    assert f"/resolve {ticket_id}" in message              # the way out
    # And it is on the record, not only in a chat message.
    assert any((e["note"] or "").startswith(basing.CONFLICT_NOTE_PREFIX)
               for e in history(ticket_id))


def test_prerequisites_conflicting_with_each_other_say_so(db, repo, cfg, webhook, posts):
    """The other half of the distinction the message has to draw."""
    first, _ = done_prerequisite(repo, title="messages",
                                 files={"app.py": "import a, messages\n"})
    second, _ = done_prerequisite(repo, title="profiles",
                                  files={"app.py": "import a, profiles\n"})
    ticket_id = tracker.add_backlog_item("realtime", "d", str(repo), "",
                                         acceptance_criteria="- x",
                                         depends_on=[first, second], project_id=1)
    tickets.move(ticket_id, tickets.READY, "tester")
    posts.clear()

    with pytest.raises(basing.ConflictingPrerequisites):
        basing.ensure_buildable(ticket_id)

    assert len(posts) == 1, contents(posts)
    message = contents(posts)[0]
    assert "conflict with each other" in message
    assert f"#{first}" in message and f"#{second}" in message
    assert f"/resolve {ticket_id}" in message


# -- criterion 2: one code path, not one per surface --------------------------

def test_every_surface_posts_through_the_same_recorded_conflict(
        db, repo, cfg, webhook, posts, monkeypatch):
    """The push hangs off the recorded conflict, so no surface carries its own.

    The lifecycle note alone posts; and every surface that refuses a build for
    conflicts — MCP/Discord dispatch, the dashboard's launcher, and the
    `eng-crew ticket <id>` the CLI runs — posts by recording that same note,
    with nothing of its own.
    """
    pytest.importorskip("fastapi")
    from eng_crew import dispatch
    from eng_crew import run as run_mod
    from eng_crew.dashboard.routers import runs as runs_router

    ticket_id, first, _ = conflicted_dependent(repo)
    posts.clear()

    # 1. the record alone, with no surface in sight
    recorded: list[tuple] = []
    real_ticket_event = notify.ticket_event
    monkeypatch.setattr(notify, "ticket_event",
                        lambda *a, **kw: recorded.append((a, kw)) or True)
    tickets.note(ticket_id, basing.ACTOR,
                 f"{basing.CONFLICT_NOTE_PREFIX} #{first}'s branch conflicts with HEAD")
    assert recorded and recorded[0][0][1] == notify.CONFLICT
    monkeypatch.setattr(notify, "ticket_event", real_ticket_event)

    # 2. each surface, reaching the same code
    def by_cli():
        with pytest.raises(basing.ConflictingPrerequisites):
            run_mod.run_ticket(ticket_id, actor="cli", settings=cfg)

    for refuse in (
        lambda: dispatch.build_ticket(ticket_id, actor="tester", settings=cfg),
        lambda: runs_router.launch_ticket_run(ticket_id, actor="tester"),
        by_cli,
    ):
        posts.clear()
        notify.forget_posted()
        refuse()
        assert len(posts) == 1, contents(posts)
        assert f"/resolve {ticket_id}" in contents(posts)[0]


# -- criterion 3: /resolve is the same resolution, in the asker's name --------

def test_discord_resolve_runs_the_same_resolution_credited_to_the_asker(
        db, repo, cfg, agent):
    ticket_id, first, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}

    ok, _ = discord_bot.resolve_start(ticket_id, "alway", cfg)
    message = discord_bot.resolve_outcome(ticket_id, "alway", cfg)

    assert ok
    # the same resolution `eng-crew resolve` makes: the next build forks from it
    assert basing.resolve_base(ticket_id).ref == basing.resolved_branch(ticket_id)
    assert "resolved" in message.lower()
    # credited to the Discord user, not to a robot
    outcome = [e for e in history(ticket_id)
               if (e["note"] or "").startswith(resolve.NOTE_PREFIX)]
    assert outcome and outcome[-1]["actor"] == "alway"
    # charged to the ticket's budget
    assert tracker.ticket_spend(ticket_id) == pytest.approx(agent["cost"])
    # and it started no build
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.READY


# -- criterion 4: it replies at once, and reports the outcome -----------------

def test_discord_resolve_replies_at_once_then_posts_the_outcome(
        db, repo, cfg, agent, webhook, posts):
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}

    posts.clear()
    ok, immediate = discord_bot.resolve_start(ticket_id, "alway", cfg)
    assert ok and agent["calls"] == []           # nothing has run yet
    assert "resolv" in immediate.lower()

    posts.clear()
    message = discord_bot.resolve_outcome(ticket_id, "alway", cfg)

    branch = basing.resolved_branch(ticket_id)
    assert branch in message
    assert "/build" in message or "next build" in message.lower()
    assert len(posts) == 1, contents(posts)
    assert branch in contents(posts)[0]


def test_a_resolution_that_fails_is_reported_with_its_reason(
        db, repo, cfg, agent, webhook, posts):
    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {}                          # the agent leaves the markers in
    posts.clear()

    message = discord_bot.resolve_outcome(ticket_id, "alway", cfg)

    assert "not resolved" in message.lower()
    assert "app.py" in message
    from eng_crew import git_skill          # the branch was thrown away
    assert not git_skill.branch_exists(str(repo), basing.resolved_branch(ticket_id))
    assert len(posts) == 1, contents(posts)
    assert "app.py" in contents(posts)[0]


def test_a_resolution_that_breaks_the_checks_is_reported(
        db, repo, cfg, agent, monkeypatch):
    from eng_crew import verify as verify_mod

    ticket_id, _, _ = conflicted_dependent(repo)
    agent["write"] = {"app.py": "import a, messages, profiles\n"}
    monkeypatch.setattr(verify_mod, "verify", lambda *a, **kw: _Broken())

    message = discord_bot.resolve_outcome(ticket_id, "alway", cfg)

    assert "not resolved" in message.lower()
    assert "the project's checks" in message


class _Broken:
    passed = False

    def summary(self):
        return "FAILED (pytest)"


# -- criterion 5: a spent budget refuses it ----------------------------------

def test_discord_resolve_refuses_a_spent_budget(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo, budget_usd=1.0)
    spent = tracker.create_run("earlier", str(repo), ticket_id=ticket_id)
    tracker.log_event(spent, 0, "single_agent", LLMResult(text="", cost_usd=2.0))
    tracker.finish_run(spent, status="failed")

    ok, message = discord_bot.resolve_start(ticket_id, "alway", cfg)

    assert not ok
    assert "budget" in message.lower()
    assert agent["calls"] == []


# -- criterion 6: nothing to resolve, no such ticket, already running --------

def test_discord_resolve_refuses_a_ticket_with_no_conflict(db, repo, cfg, agent):
    first, _ = done_prerequisite(repo, title="api", files={"api.py": "E = 1\n"})
    ticket_id = tracker.add_backlog_item("web", "d", str(repo), "",
                                         acceptance_criteria="- x", depends_on=[first])
    tickets.move(ticket_id, tickets.READY, "tester")

    ok, message = discord_bot.resolve_start(ticket_id, "alway", cfg)

    assert not ok
    assert "no conflict" in message.lower()
    assert agent["calls"] == []
    assert tracker.get_ticket_runs(ticket_id) == []


def test_discord_resolve_refuses_a_ticket_that_does_not_exist(db, cfg, agent):
    ok, message = discord_bot.resolve_start(4242, "alway", cfg)

    assert not ok
    assert "4242" in message
    assert agent["calls"] == []


def test_discord_resolve_refuses_a_resolution_already_in_progress(db, repo, cfg, agent):
    ticket_id, _, _ = conflicted_dependent(repo)

    with resolve._resolving_lock:
        resolve._resolving.add(ticket_id)
    try:
        ok, message = discord_bot.resolve_start(ticket_id, "alway", cfg)
    finally:
        with resolve._resolving_lock:
            resolve._resolving.discard(ticket_id)

    assert not ok
    assert "already being resolved" in message
    assert agent["calls"] == []


# -- criterion 7: these messages are #100802's messages ----------------------

def test_conflict_and_resolution_messages_are_notifications_like_the_rest(
        db, repo, cfg, agent, webhook, posts, monkeypatch, caplog):
    from eng_crew import config

    monkeypatch.setattr(config.settings, "discord_notify_user_id", USER_ID)
    ticket_id, _, _ = conflicted_dependent(repo, title="fix @everyone pings")
    agent["write"] = {"app.py": "import a, messages, profiles\n"}
    posts.clear()

    with pytest.raises(basing.ConflictingPrerequisites):
        basing.ensure_buildable(ticket_id)
    discord_bot.resolve_outcome(ticket_id, "alway", cfg)

    assert len(posts) == 2, contents(posts)
    for sent in posts:
        payload = sent["payload"]
        # the configured user, and nobody else
        assert payload["allowed_mentions"] == {"parse": [], "users": [USER_ID]}
        assert f"<@{USER_ID}>" in payload["content"]
        # a mention sitting in the ticket's own title pings nobody
        assert "@everyone" not in payload["content"]
        assert sent["timeout"] == notify.TIMEOUT_SECONDS

    # best-effort: a webhook that is down breaks neither the refusal nor the
    # resolution, and never writes the URL anywhere
    def explode(url, **kw):
        raise RuntimeError(f"cannot reach {url}")

    monkeypatch.setattr(notify.httpx, "post", explode)
    notify.forget_posted()
    with caplog.at_level(logging.DEBUG):
        # Both facts are still recorded, and neither raises.
        assert basing.record_conflict(basing.ConflictingPrerequisites(
            ticket_id, [1], "app.py", with_head=True))
        assert tickets.note(
            ticket_id, "alway",
            f"{resolve.NOTE_PREFIX} resolved the conflicts merging #1 onto HEAD")
    assert WEBHOOK not in caplog.text
    assert "s3cret-t0ken-nobody-may-log" not in caplog.text
    assert WEBHOOK not in "\n".join(e["note"] or "" for e in history(ticket_id))


# -- criterion 8: no robot may ask for it ------------------------------------

@pytest.mark.parametrize("robot", sorted(tickets.ROBOTS))
def test_no_robot_actor_can_trigger_a_resolution(db, repo, cfg, agent, robot):
    ticket_id, _, _ = conflicted_dependent(repo)

    ok, message = discord_bot.resolve_start(ticket_id, robot, cfg)
    assert not ok
    assert robot in message

    with pytest.raises(resolve.ResolveRefused):
        resolve.resolve(ticket_id, cfg, actor=robot)
    assert agent["calls"] == []


# -- criterion 9: it is documented -------------------------------------------

@pytest.mark.parametrize("doc", ["CLAUDE.md", "README.md"])
def test_resolve_is_documented(doc):
    text = (Path(__file__).resolve().parents[1] / doc).read_text(encoding="utf-8")
    assert "/resolve <ticket_id>" in text


def test_the_bots_command_help_lists_resolve():
    assert "/resolve" in (discord_bot.__doc__ or "")
