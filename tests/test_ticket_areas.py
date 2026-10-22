"""Tickets carry an area, and the roadmap reads them that way.

Priority said what order to pull work in and dependencies said what had to come
first, but neither said *what part of the product* a ticket belongs to. An area
is that: a short per-project label the planner assigns and a person can change.

The roadmap is what the label is for — one column per area, the tickets in an
order where a prerequisite is always above the ticket that waits on it, and each
one marked with the only four things worth knowing at a glance: it is done, it
can be started now, it is in flight, or it is blocked by work that is unfinished.

These assert the label survives storage and changes (with who changed it
recorded), that the planner fills it in from a small consistent set, and that
every surface reading it — the roadmap view, Discord's `/roadmap`, the
discussion agent's tracker section — reports the same grouping and states.
"""
from __future__ import annotations

import json

import pytest

from eng_crew import discord_bot, discuss, planning, tickets, tracker
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "areas.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path, wip_limit=3, ticket_budget_usd=15.0)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


CFG = {"name": "Demo", "path": "/proj"}


def make_ticket(project="/proj", *, criteria="- it works", priority=50,
                title="do a thing", depends_on=None, area=None) -> int:
    return tracker.add_backlog_item(
        title, "the details", project, "", priority=priority,
        acceptance_criteria=criteria, depends_on=depends_on, area=area,
    )


def finish(ticket_id: int, final: str = tickets.DONE) -> None:
    """Walk a ticket from backlog to done (or on to shipped)."""
    for status in (tickets.READY, tickets.IN_PROGRESS, tickets.IN_REVIEW,
                   tickets.IN_QA, tickets.DONE, tickets.SHIPPED):
        tickets.move(ticket_id, status, "tester")
        if status == final:
            return


# --- storage ------------------------------------------------------------


def test_a_ticket_stores_its_area_and_one_without_is_unassigned(db):
    labelled = make_ticket(area="Engine")
    bare = make_ticket()

    assert tracker.get_backlog_item(labelled)["area"] == "Engine"
    assert tickets.area_of(tracker.get_backlog_item(labelled)) == "Engine"
    # A ticket written before areas existed keeps working and has a group.
    assert tickets.area_of(tracker.get_backlog_item(bare)) == tickets.UNASSIGNED
    assert tickets.move(bare, tickets.READY, "tester")["status"] == tickets.READY


def test_an_area_can_be_changed_and_the_change_is_recorded_with_the_actor(db):
    ticket_id = make_ticket(area="Engine")

    assert tickets.set_area(ticket_id, "Website", actor="dana") == "Website"
    assert tracker.get_backlog_item(ticket_id)["area"] == "Website"

    history = tickets.events(ticket_id)
    assert history[-1]["actor"] == "dana"
    assert "Engine" in history[-1]["note"] and "Website" in history[-1]["note"]
    # Changing the area is not a status change.
    assert history[-1]["from_status"] == history[-1]["to_status"] == tickets.BACKLOG

    # Clearing it puts the ticket back in the Unassigned group.
    tickets.set_area(ticket_id, "", actor="dana")
    assert tickets.area_of(tracker.get_backlog_item(ticket_id)) == tickets.UNASSIGNED


def test_setting_the_area_of_a_ticket_that_does_not_exist_is_refused(db):
    with pytest.raises(tickets.TransitionRefused):
        tickets.set_area(9999, "Engine", actor="dana")


# --- the planner fills it in --------------------------------------------


def test_the_planner_gives_every_ticket_an_area_from_a_small_set(
    db, project, cfg, monkeypatch
):
    plan = {
        "areas": ["Engine", "Website"],
        "tickets": [
            {"title": "Schema", "acceptance_criteria": "- tables exist",
             "priority": 10, "area": "Engine"},
            {"title": "API", "acceptance_criteria": "- endpoints respond",
             "priority": 20, "area": "engine", "depends_on": [1]},
            {"title": "Landing page", "acceptance_criteria": "- it renders",
             "priority": 30, "area": "Website"},
            # No area of its own: the planner's own set is the fallback, not
            # a fourth name invented per ticket.
            {"title": "Docs", "acceptance_criteria": "- they read", "priority": 40},
        ]
    }

    class FakePlanner:
        def __init__(self, settings=None):
            pass

        def call(self, prompt, role=None, **kw):
            return LLMResult(text=json.dumps(plan))

    monkeypatch.setattr(planning, "BaseAgent", FakePlanner)
    created = planning.plan_tickets("ship v2", project, settings=cfg)

    areas = [tickets.area_of(t) for t in created]
    assert all(a for a in areas), "every planned ticket carries an area"
    # Reused, not re-invented: "engine" and "Engine" are the same area.
    assert areas[0] == areas[1] == "Engine"
    assert areas[2] == "Website"
    assert len(set(areas)) <= 3
    # The prompt is what asks for the consistent set, so it has to mention it.
    assert "area" in planning.prompts.render(
        "plan-tickets", project_context="", goal="x"
    ).lower()


# --- the roadmap --------------------------------------------------------


def test_the_roadmap_groups_by_area_with_unassigned_last(db):
    make_ticket(title="engine bit", area="Engine")
    make_ticket(title="site bit", area="Website")
    make_ticket(title="stray bit")

    board = tickets.roadmap("/proj")
    assert [area for area, _ in board["groups"]] == [
        "Engine", "Website", tickets.UNASSIGNED,
    ]
    assert [c["title"] for c in board["groups"][2][1]] == ["stray bit"]


def test_inside_an_area_a_prerequisite_comes_before_what_waits_on_it(db):
    # Filed, and prioritised, in the wrong order on purpose.
    last = make_ticket(title="UI", area="Engine", priority=10)
    first = make_ticket(title="Schema", area="Engine", priority=90)
    middle = make_ticket(title="API", area="Engine", priority=50,
                         depends_on=[first])
    tickets.set_dependencies(last, [middle], "tester")

    board = tickets.roadmap("/proj")
    order = [c["id"] for c in dict(board["groups"])["Engine"]]
    assert order.index(first) < order.index(middle) < order.index(last)


def test_each_card_says_whether_it_is_done_ready_in_flight_or_blocked(db):
    done = make_ticket(title="Schema", area="Engine", priority=10)
    finish(done)
    in_flight = make_ticket(title="API", area="Engine", priority=20)
    tickets.move(in_flight, tickets.READY, "tester")
    tickets.move(in_flight, tickets.IN_PROGRESS, "tester")
    startable = make_ticket(title="Docs", area="Engine", priority=30,
                            depends_on=[done])
    blocked = make_ticket(title="UI", area="Engine", priority=40,
                          depends_on=[in_flight])

    cards = {c["id"]: c for c in dict(tickets.roadmap("/proj")["groups"])["Engine"]}
    assert cards[done]["state"] == tickets.ROADMAP_DONE
    assert cards[in_flight]["state"] == tickets.ROADMAP_IN_FLIGHT
    # Its one prerequisite is done, so it can be picked up now.
    assert cards[startable]["state"] == tickets.ROADMAP_READY
    assert cards[blocked]["state"] == tickets.ROADMAP_BLOCKED
    # A blocked card names what it waits on, not just that it waits.
    assert [p["id"] for p in cards[blocked]["unmet_prerequisites"]] == [in_flight]
    # Every card carries what a card shows.
    for card in cards.values():
        assert card["status"] in tickets.STATUSES
        assert card["title"] and card["id"]

    assert tickets.roadmap("/proj")["counts"] == {
        tickets.ROADMAP_DONE: 1, tickets.ROADMAP_READY: 1,
        tickets.ROADMAP_IN_FLIGHT: 1, tickets.ROADMAP_BLOCKED: 1,
    }


def test_the_roadmap_is_scoped_to_one_project(db):
    make_ticket(title="ours", area="Engine")
    make_ticket(title="theirs", project="/other", area="Engine")

    cards = dict(tickets.roadmap("/proj")["groups"])["Engine"]
    assert [c["title"] for c in cards] == ["ours"]


# --- Discord /roadmap ---------------------------------------------------


def test_the_roadmap_command_renders_the_grouping_the_states_and_a_tally(db):
    done = make_ticket(title="Schema", area="Engine", priority=10)
    finish(done)
    in_flight = make_ticket(title="API", area="Engine", priority=20)
    tickets.move(in_flight, tickets.READY, "tester")
    tickets.move(in_flight, tickets.IN_PROGRESS, "tester")
    blocked = make_ticket(title="UI", area="Website", priority=30,
                          depends_on=[in_flight])
    make_ticket(title="stray", priority=40)

    title, body = discord_bot.render_roadmap(CFG)
    assert "Demo" in title and "Roadmap" in title
    # The tally of states, as a header.
    assert "done 1" in body and "ready to start 1" in body
    assert "in flight 1" in body and "blocked 1" in body
    # One section per area, Unassigned included.
    assert "Engine" in body and "Website" in body and tickets.UNASSIGNED in body
    assert f"#{done}" in body and f"#{blocked}" in body
    # A blocked card names the unfinished prerequisite.
    assert f"blocked by #{in_flight}" in body


def test_a_long_roadmap_is_bounded_and_says_how_much_it_left_out(db):
    for i in range(40):
        make_ticket(title=f"ticket {i} " + "x" * 60, area=f"Area {i % 3}",
                    priority=i)

    _, body = discord_bot.render_roadmap(CFG, limit=10)
    assert len(body) <= 4000
    assert "more" in body


def test_an_empty_roadmap_says_so_rather_than_rendering_nothing(db):
    title, body = discord_bot.render_roadmap(CFG)
    assert "Demo" in title
    assert "No tickets" in body


def test_rendering_the_roadmap_never_changes_a_ticket(db):
    ticket_id = make_ticket(title="Schema", area="Engine")
    before = tracker.get_backlog_item(ticket_id)
    history = tickets.events(ticket_id)

    discord_bot.render_roadmap(CFG)

    assert tracker.get_backlog_item(ticket_id) == before
    assert tickets.events(ticket_id) == history


def test_discord_can_set_a_ticket_area_crediting_the_person_who_did(db):
    ticket_id = make_ticket(title="Schema", area="Engine")

    message = discord_bot.area_reply(ticket_id, "Website", "dana")

    assert "Website" in message
    assert tracker.get_backlog_item(ticket_id)["area"] == "Website"
    assert tickets.events(ticket_id)[-1]["actor"] == "dana"
    assert "no ticket" in discord_bot.area_reply(9999, "Website", "dana").lower()


# --- the discussion agent -----------------------------------------------


def test_the_tracker_section_groups_by_area_when_areas_are_set(db):
    make_ticket(title="engine bit", area="Engine", priority=10)
    make_ticket(title="site bit", area="Website", priority=20)

    section = discuss.tracker_section("/proj", "Demo")
    assert "[Engine]" in section and "[Website]" in section
    assert section.index("[Engine]") < section.index("engine bit")
    assert section.index("[Website]") < section.index("site bit")


def test_with_no_areas_set_the_tracker_section_still_groups_by_status(db):
    ticket_id = make_ticket(title="only bit", priority=10)
    tickets.move(ticket_id, tickets.READY, "tester")

    section = discuss.tracker_section("/proj", "Demo")
    assert "[ready]" in section and "only bit" in section
