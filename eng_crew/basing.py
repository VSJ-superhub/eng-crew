"""What a ticket's run forks from — its prerequisites' work, not the code behind it.

A prerequisite counts as met once it is ``done``, but a done ticket's work lives
on its run's branch, and nothing merges that branch for you. So a dependent
ticket forking from the project checkout's HEAD started from code where its
prerequisite had never happened: the agent read a codebase missing the schema it
was told to build an API on, and built it again.

The rule, in order:

  * The caller asked for a base (``eng-crew ticket --base``, ``base_branch`` to
    ``run_task``) — that wins, untouched. An explicit base is a decision.
  * Every prerequisite's branch is already in HEAD — nothing to do, fork from
    HEAD exactly as before. This is the normal state once work is merged.
  * A prerequisite's branch that another unmerged prerequisite's branch already
    contains (one run stacked on the other) is carried by that branch and is
    not merged a second time.
  * Exactly one prerequisite branch is left and it already contains HEAD —
    fork from that branch.
  * Otherwise — several branches, or one that HEAD has moved on from — merge
    them onto HEAD on a branch of their own and fork from that, so the run sees
    the prerequisites' work *and* whatever landed on HEAD meanwhile. If they
    cannot be combined, the build is refused *before* it starts, naming the
    prerequisites that conflict — with each other, or with HEAD — and nothing
    is left behind: a half-merged base is worse than no build.

:func:`resolve_base` is called by the one command every surface launches
(``eng-crew ticket <id>``), so the CLI, the dashboard, Discord's ``/build`` and
a queue pull cannot disagree about it. A surface that claims the ticket before
launching calls :func:`ensure_buildable` first, so a refusal reaches the person
who asked instead of a log, and the ticket stays where it was.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Branch prefix for the branch that merges several prerequisites onto HEAD.
#: Distinct from a run's own prefix: this one carries no new work, only the
#: combination a run is about to be built on.
BASE_PREFIX = "eng-crew/base"


@dataclass
class Base:
    """Where a run starts, and why — recorded on the run.

    ``ref`` is None for "the project checkout's HEAD", which is what every run
    with nothing to stack on gets, and what the pipeline already did.
    """
    ref: str | None = None
    reason: str = "the project checkout's HEAD"
    #: The prerequisite ids whose work this base carries, in dependency order.
    prerequisites: list[int] = field(default_factory=list)


class ConflictingPrerequisites(Exception):
    """Prerequisite branches that cannot be merged onto HEAD.

    Either they conflict with each other, or — ``with_head`` — one of them
    conflicts with HEAD itself, which has moved on since it was branched.

    Not a transition refusal: the lifecycle is happy — the prerequisites *are*
    done. It is the code that does not combine, and a person has to merge it.
    """

    def __init__(
        self, ticket_id: int, conflicting: list[int], detail: str = "",
        *, with_head: bool = False,
    ):
        self.ticket_id = ticket_id
        self.conflicting = conflicting
        self.with_head = with_head
        named = ", ".join(f"#{i}" for i in conflicting)
        if with_head:
            what = (f"{named}'s branch conflicts with HEAD — merge it into HEAD "
                    f"yourself first")
        else:
            what = f"{named} conflict with each other — merge them yourself first"
        super().__init__(
            f"ticket {ticket_id} cannot be built on its prerequisites: {what}"
            + (f" ({detail})" if detail else "")
        )


def _as_ticket(ticket: dict | int) -> dict:
    from . import tracker

    if isinstance(ticket, int):
        return tracker.get_backlog_item(ticket) or {}
    return ticket or {}


def _unmerged_branch(project_path: str, prerequisite_id: int) -> str | None:
    """The branch holding a prerequisite's work, if HEAD does not have it yet.

    The last run of the ticket that left a branch is the one that counts: an
    earlier attempt was superseded by the one the reviewer approved.
    """
    from . import git_skill, tracker

    for run in reversed(tracker.get_ticket_runs(prerequisite_id)):
        branch = (run.get("git_branch") or "").strip()
        if not branch or not git_skill.branch_exists(project_path, branch):
            continue
        if git_skill.is_contained_in(project_path, branch, "HEAD"):
            return None                 # already in HEAD: nothing to stack on
        return branch
    return None


def resolve_base(ticket: dict | int, *, explicit_base: str = "") -> Base:
    """Where this ticket's run should fork from. Never touches the checkout.

    Raises :class:`ConflictingPrerequisites` when the unmerged prerequisite
    branches do not combine with each other or with HEAD.
    """
    from . import git_skill, tickets

    explicit = (explicit_base or "").strip()
    if explicit:
        return Base(explicit, f"the caller asked to base this run on {explicit}")

    row = _as_ticket(ticket)
    ticket_id = int(row.get("id") or 0)
    project_path = row.get("project_path") or ""
    prereqs = tickets.dependencies(row)
    if not prereqs or not project_path or not git_skill.is_git_repo(project_path):
        return Base()

    unmerged: list[tuple[int, str]] = []
    for prerequisite_id in prereqs:
        try:
            branch = _unmerged_branch(project_path, prerequisite_id)
        except Exception as exc:
            # Basing is an improvement on forking from HEAD, not a precondition
            # for building. An unreadable repo must not block the work.
            log.warning("could not read #%s's branch: %s", prerequisite_id, exc)
            continue
        if branch:
            unmerged.append((prerequisite_id, branch))

    if not unmerged:
        return Base(
            None,
            "the project checkout's HEAD — every prerequisite's work is already in it"
            if prereqs else "the project checkout's HEAD",
            prereqs,
        )

    ids = [i for i, _ in unmerged]
    # A branch another prerequisite's branch already contains (its run was
    # stacked on this one) is carried by that branch; merging it again adds
    # nothing, and would only muddy who a conflict is with.
    refs = list(dict.fromkeys(
        b for _, b in unmerged
        if not any(other != b and git_skill.is_contained_in(project_path, b, other)
                   for _, other in unmerged)
    ))

    if len(refs) == 1 and git_skill.is_contained_in(project_path, "HEAD", refs[0]):
        named = ", ".join(f"#{i}" for i in ids)
        return Base(
            refs[0],
            f"{named}'s branch {refs[0]} — the work is done but not in HEAD yet"
            if len(ids) == 1 else
            f"{refs[0]} — it carries {named}, done but not in HEAD yet",
            ids,
        )

    branch = git_skill.make_branch_name(BASE_PREFIX, f"ticket-{ticket_id}")
    failed, merged = git_skill.merge_branches(project_path, branch, refs)
    if failed:
        failed_ids = [i for i, b in unmerged if b == failed]
        # The first branch merged onto HEAD alone, so a failure there can only
        # be with HEAD; a later one may still be, if it fails against HEAD too.
        if not merged or git_skill.merges_cleanly(project_path, "HEAD", failed) is False:
            raise ConflictingPrerequisites(ticket_id, failed_ids, failed, with_head=True)
        # Otherwise whoever merged cleanly before the failure is who it
        # conflicts with.
        conflicting = [i for i, b in unmerged if b == failed or b in merged]
        raise ConflictingPrerequisites(ticket_id, conflicting or ids, failed)

    named = ", ".join(f"#{i}" for i in ids)
    if len(refs) == 1:
        reason = (f"{branch} — {named} {'is' if len(ids) == 1 else 'are'} done "
                  f"but not in HEAD yet, and HEAD has moved on since, so "
                  f"{refs[0]} was merged onto it")
    else:
        reason = (f"{branch} — {named} are done but not in HEAD yet, so their "
                  f"branches were merged onto it")
    return Base(branch, reason, ids)


def ensure_buildable(ticket: dict | int, *, explicit_base: str = "") -> None:
    """Raise if this ticket cannot be based on its prerequisites. Creates nothing.

    The pre-flight for a surface that claims a ticket before launching the
    build: the refusal has to arrive before the claim, or the ticket holds a WIP
    slot for a build that was never going to start.
    """
    from . import git_skill

    base = resolve_base(ticket, explicit_base=explicit_base)
    if base.ref and base.ref.startswith(BASE_PREFIX + "/"):
        # This check made a merge branch to prove the merge works; the build
        # will make its own. Leaving this one would litter the repo.
        row = _as_ticket(ticket)
        try:
            git_skill.delete_branch(row.get("project_path") or "", base.ref)
        except Exception as exc:
            log.warning("could not clean up the trial base %s: %s", base.ref, exc)
