"""
eng-crew Discord bot — dispatch tasks to your engineering team from mobile.

Thin, single-agent-first bot:
  /task project:<alias> task:<desc>   Dispatch a task (with a Run/Cancel confirm)
  /ideate project:<alias>             Ideate with the manager. It writes tickets,
                                      not builds: accepting a proposal files a
                                      backlog ticket with acceptance criteria and
                                      starts nothing. Approving that ticket to
                                      `ready` is what releases it to the queue.
  /endideate                          Leave ideation mode (and remember the session)
  /backlog project:<alias>            The project's ticket tracker — WIP, the
                                      queue, and every ticket by status
  /ticket id:<n>                      One ticket in full: criteria, spend,
                                      review verdict, runs, history
  /roadmap project:<alias>            The same tickets grouped by area, in
                                      dependency order, each one done, ready to
                                      start, in flight, or blocked by what is
                                      unfinished
  /area ticket_id:<id> area:<name>    Put a ticket in an area, or clear it
  /queue project:<alias>              That project's ticket queue — id, priority,
                                      status, acceptance criteria, spend, WIP
  /build ticket_id:<id>               Build a ticket that already exists. A
                                      backlog ticket is approved to `ready` in
                                      your name first; nothing new is filed.
  /status                             Recent and active runs
  /projects                           Configured project aliases
  /newtopic                           Forget this channel's conversation
  @mention / DM                       Ask anything and it is a conversation.
                                      Name a project alias with a directive
                                      ("resolvemind add a logout button") and it
                                      becomes a build, behind a confirm button.

There is no plan-approval gate: eng-crew's default single-agent tier plans
internally and applies the change on an isolated ai-team/* branch. You review
the diff afterward. (The opt-in multi-agent graph keeps its dashboard HITL gate.)

Run:  python -m eng_crew.discord_bot   (requires DISCORD_BOT_TOKEN in .env)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .config import load_env_into_environ

load_env_into_environ()

import discord
from discord import app_commands

# Project aliases are one user's own list, so they live with that user's data,
# never in the package. ENG_CREW_BOT_CONFIG points anywhere else.
def bot_config_path() -> Path:
    from .config import DATA_DIR

    override = os.environ.get("ENG_CREW_BOT_CONFIG")
    return Path(override).expanduser() if override else Path(DATA_DIR) / "bot_config.json"

# ── Per-channel ideation state ──────────────────────────────────────────────────
# When a channel is in ideation mode, @mention/DM messages are routed to the
# grounded manager instead of being dispatched as a task.
_ideate_project: dict[int, dict] = {}   # channel_id -> project cfg
_ideate_history: dict[int, list] = {}   # channel_id -> [{"role","content"}]
_ideate_mode: set[int] = set()          # channel_ids currently in ideation mode

# ── Per-channel discussion memory ───────────────────────────────────────────────
# Anything that is not a build request is a conversation, and conversations need
# continuity — "why?" as a follow-up has to know what it follows.
_chat_history: dict[int, list] = {}     # channel_id -> [{"role","content"}]


# ── Project resolution ──────────────────────────────────────────────────────────
def load_projects() -> dict:
    """Merge bot_config.json aliases with DB-registered projects (aliases win)."""
    projects: dict = {}
    path = bot_config_path()
    if path.exists():
        try:
            projects = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            projects = {}

    known = {v["path"].rstrip("/\\").lower() for v in projects.values() if v.get("path")}
    try:
        from .tracker import list_projects as _list
        for p in _list(active_only=False):
            path = (p.get("project_path") or "").rstrip("/\\")
            if not path or path.lower() in known:
                continue
            key = (p.get("name") or f"project_{p.get('id')}").lower().replace(" ", "_")
            if key in projects:
                key = f"{key}_{p.get('id')}"
            projects[key] = {
                "name": p.get("name") or key,
                "path": path,
                "claude_md": p.get("claude_md_path") or path + "/CLAUDE.md",
            }
            known.add(path.lower())
    except Exception as e:
        # Aliases still work, but a project registered by planning or a run would
        # silently go missing from /projects — say so rather than shrug.
        print(f"[discord_bot] could not read registered projects: {e}", file=sys.stderr)
    return projects


def _unknown_project(key: str, projects: dict) -> str:
    """The one reply every project-taking command gives for a name it has not got."""
    available = ", ".join(f"`{k}`" for k in projects)
    return (
        f"❓ Unknown project `{key}`. Available: "
        + (available or f"none — register one on the dashboard, or add it to {bot_config_path()}")
    )


# ── The ticket queue, from Discord ──────────────────────────────────────────────
# A project is planned into tickets, not into a build (see eng_crew/planning.py),
# so a phone with only /task on it could file new work but never touch the queue
# that planning produced. These two read and pull from that queue, under the same
# lifecycle, WIP limit and budget as the dashboard and the CLI.

_QUEUE_ROWS = 20


def queue_lines(cfg: dict, settings=None) -> list[str]:
    """A project's queue as Discord lines — the data `eng-crew queue` prints."""
    from . import budget as budget_mod
    from . import tickets, tracker

    path = cfg["path"]
    state = tickets.wip(path, settings)
    lines = [
        f"**{cfg.get('name') or path}** — WIP {state['in_flight']}/{state['limit']} "
        f"in flight ({state['slots']} slot(s) free)"
    ]
    rows = tracker.list_project_tickets(path)
    if not rows:
        lines.append("_No tickets for this project yet._")
        return lines

    for ticket in rows[:_QUEUE_ROWS]:
        spend = budget_mod.annotate(ticket, settings)
        money = f"${spend['spent_usd']:.2f}/${spend['budget_usd']:.2f}"
        if spend["budget_exhausted"]:
            money += " ⚠️"
        criteria = "AC ✅" if (ticket.get("acceptance_criteria") or "").strip() else "AC ❌"
        lines.append(
            f"`#{ticket['id']}` p{ticket.get('priority', '')} · "
            f"**{ticket.get('status', '')}** · {str(ticket.get('title', ''))[:60]} · "
            f"{criteria} · {money}{_depends(ticket)}"
        )
    if len(rows) > _QUEUE_ROWS:
        lines.append(f"_…and {len(rows) - _QUEUE_ROWS} more — see the dashboard._")
    lines.append("Build one with `/build <ticket_id>`.")
    return lines


def build_reply(ticket_id: int, actor: str, settings=None) -> str:
    """Build an existing ticket and say what happened. Never files a new one."""
    from . import dispatch

    result = dispatch.build_ticket(ticket_id, actor=actor, settings=settings)
    if not result.ok:
        return f"❌ {result.detail}"
    if not result.started:
        return f"⏸️ {result.detail}"
    return (
        f"🚀 Building ticket **#{ticket_id}** — "
        f"{result.wip.get('in_flight', '?')}/{result.wip.get('limit', '?')} in flight. "
        f"Track it on the dashboard; the result lands on an `ai-team/*` branch."
    )


# ── Task execution ──────────────────────────────────────────────────────────────
def task_reply(cfg: dict, task: str, actor: str, settings=None) -> str:
    """File a free-text task as a ticket and build it, as `run_task` does.

    The confirm card used to call the pipeline directly, so a Discord task got
    no ticket — no acceptance criteria, no independent review, no budget, no
    retro. Going through dispatch gives it all four, and the click is the
    approval that readies the ticket, credited to whoever clicked.
    """
    from . import dispatch

    result = dispatch.dispatch_task(cfg["path"], task, actor=actor, settings=settings)
    if not result.ok:
        return f"❌ {result.detail}"
    lines = []
    if result.started:
        lines.append(
            f"🚀 Filed and building ticket **#{result.ticket_id}** — "
            f"{result.wip.get('in_flight', '?')}/{result.wip.get('limit', '?')} in flight. "
            f"Track it on the dashboard; the result lands on an `ai-team/*` branch."
        )
    else:
        lines.append(f"⏸️ {result.detail}")
    if result.criteria_source == "manager":
        lines.append("The Manager drafted its acceptance criteria — `/ticket "
                     f"{result.ticket_id}` shows them.")
    if result.warning:
        lines.append(f"⚠️ {result.warning}")
    return "\n".join(lines)


# ── Confirm card ────────────────────────────────────────────────────────────────
class ConfirmView(discord.ui.View):
    def __init__(self, cfg: dict, task: str):
        super().__init__(timeout=300)
        self._cfg = cfg
        self._task = task

    @discord.ui.button(label="🚀 Run it", style=discord.ButtonStyle.success)
    async def run(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(
                content="⏳ Filing a ticket with eng-crew...", view=self
            )
            self.stop()
            actor = str(getattr(interaction.user, "name", None) or interaction.user)
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(
                bot.executor, task_reply, self._cfg, self._task, actor
            )
            await interaction.channel.send(reply)
        except Exception as e:
            try:
                await interaction.edit_original_response(content=f"❌ Error: {e}", view=None)
            except Exception:
                pass

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.edit_message(content="❌ Cancelled.", view=None)
        except Exception:
            await interaction.response.send_message("❌ Cancelled.", ephemeral=True)
        self.stop()


async def _post_confirm(target, projects: dict, project_key: str, task: str) -> None:
    """Post a Run/Cancel confirmation card. `target` is a Message or Interaction-like."""
    if project_key not in projects:
        keys = ", ".join(f"`{k}`" for k in projects) or "none configured"
        await target.reply(f"❓ Unknown project `{project_key}`. Available: {keys}")
        return
    cfg = projects[project_key]
    embed = discord.Embed(
        title=f"📋 Task — {cfg.get('name', project_key)}",
        description=f"**Task:** {task}\n\n**Project:** `{cfg['path']}`",
        color=discord.Color.blue(),
    )
    embed.set_footer(text="Files a ticket and builds it on an isolated ai-team/* branch, with review and a budget.")
    await target.reply(embed=embed, view=ConfirmView(cfg, task))


# ── Ideation mode (grounded manager) ────────────────────────────────────────────

def accept_proposal(cfg: dict, proposal: dict) -> str:
    """File a manager proposal as a backlog ticket. Returns what to tell the channel.

    No build starts here — the ticket waits in the backlog until someone approves
    it to `ready`. If the manager left out acceptance criteria the ticket still
    exists, but the message says so, because the lifecycle will refuse to make it
    ready and a silently un-approvable ticket is how work disappears.
    """
    from . import manager

    result = manager.accept(proposal, cfg["path"])
    ticket = result["ticket"]
    lines = [
        f"📋 Filed ticket **#{ticket.get('id', result['ticket_id'])}** in the "
        f"{cfg.get('name') or 'project'} backlog: {ticket.get('title', '')}",
    ]
    if result["needs_acceptance_criteria"]:
        lines.append(f"⚠️ {result['warning']}")
    else:
        lines.append("Approve it (move it to **ready**) and the queue will build it.")
    return "\n".join(lines)


class IdeateBuildView(discord.ui.View):
    """Shown on a manager ticket proposal — file it in the backlog or keep ideating."""
    def __init__(self, cfg: dict, proposal: dict):
        super().__init__(timeout=600)
        self._cfg = cfg
        self._proposal = proposal

    @discord.ui.button(label="📋 Add to backlog", style=discord.ButtonStyle.success)
    async def backlog(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            for item in self.children:
                item.disabled = True
            loop = asyncio.get_running_loop()
            message = await loop.run_in_executor(
                bot.executor, accept_proposal, self._cfg, self._proposal
            )
            await interaction.edit_original_response(content=message, view=self)
            self.stop()
        except Exception as e:
            try:
                await interaction.edit_original_response(content=f"❌ Error: {e}", view=None)
            except Exception:
                pass

    @discord.ui.button(label="✏️ Keep refining", style=discord.ButtonStyle.secondary)
    async def refine(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Keep going — tell me more.", view=self)
        self.stop()


async def _handle_discuss(message, text: str, project) -> None:
    """Answer a question. Read-only; nothing here can start a build."""
    from . import discuss as discuss_mod

    history = _chat_history.setdefault(message.channel.id, [])
    loop = asyncio.get_running_loop()

    def _call():
        return discuss_mod.discuss(
            text,
            list(history),
            project_path=(project or {}).get("path", ""),
            project_name=(project or {}).get("name", ""),
        )

    async with message.channel.typing():
        try:
            reply = await loop.run_in_executor(bot.executor, _call)
        except Exception as e:
            await message.reply(f"❌ {e}")
            return

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply.reply})
    # Trim in place so a long-lived channel does not grow without bound.
    _chat_history[message.channel.id] = discuss_mod.trim_history(history)

    body = reply.reply or "(no response)"
    for chunk in [body[i:i + 1900] for i in range(0, len(body), 1900)]:
        await message.channel.send(chunk)


async def _handle_ideate(message: discord.Message, text: str) -> None:
    """Route a message to the grounded manager for the channel's active project."""
    cfg = _ideate_project[message.channel.id]
    history = _ideate_history.setdefault(message.channel.id, [])
    loop = asyncio.get_running_loop()

    def _call():
        from . import manager
        return manager.chat(text, list(history), cfg["path"], cfg.get("name", ""))

    async with message.channel.typing():
        try:
            reply = await loop.run_in_executor(bot.executor, _call)
        except Exception as e:
            await message.reply(f"❌ Manager error: {e}")
            return

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply.reply})

    body = reply.reply or "(no response)"
    for chunk in [body[i:i + 1900] for i in range(0, len(body), 1900)]:
        await message.channel.send(chunk)

    if reply.proposal and reply.proposal.get("title"):
        prop = reply.proposal
        desc = prop.get("description") or prop["title"]
        criteria = prop.get("acceptance_criteria") or []
        if criteria:
            desc += "\n\n**Acceptance criteria**\n" + "\n".join(f"• {c}" for c in criteria)
        else:
            from .manager import MISSING_CRITERIA_WARNING

            desc += f"\n\n⚠️ {MISSING_CRITERIA_WARNING}"
        if prop.get("rationale"):
            desc += f"\n\n*{prop['rationale']}*"
        embed = discord.Embed(
            title=f"📋 Ticket: {prop['title']}"[:256],
            description=desc[:4000],
            color=discord.Color.purple(),
        )
        await message.channel.send(embed=embed, view=IdeateBuildView(cfg, prop))


# ── Tracker views (read-only) ───────────────────────────────────────────────────
# The tracker was dashboard-only: from a phone you could file a ticket through
# /ideate and never see where it went. These render the same facts the dashboard
# and `eng-crew queue` show — status, acceptance criteria, spend against budget,
# the review verdict — as text. Nothing here moves a ticket or starts a run.

STATUS_ICONS = {
    "backlog": "📥", "ready": "📋", "in_progress": "🔄", "in_review": "👀",
    "in_qa": "🧪", "done": "✅", "rejected": "❌", "shipped": "🚢",
}


def _status_icon(status: str) -> str:
    return STATUS_ICONS.get(status or "", "•")


def _money(ticket: dict) -> str:
    from . import budget as budget_mod

    spend = budget_mod.annotate(ticket)
    money = f"${spend['spent_usd']:.2f}/${spend['budget_usd']:.2f}"
    return f"{money} ⛔" if spend["budget_exhausted"] else money


def _depends(ticket: dict) -> str:
    """A one-line note of what a ticket waits on, and whether it is blocked."""
    from . import tickets as tickets_mod

    prereqs = tickets_mod.prerequisites(ticket)
    if not prereqs:
        return ""
    unmet = [p for p in prereqs if p.get("status") not in tickets_mod.MET]
    if unmet:
        return f" · ⛔ blocked by {tickets_mod.describe_prerequisites(unmet)}"
    return " · needs " + ", ".join(f"#{p['id']}" for p in prereqs) + " ✅"


def render_backlog(cfg: dict, status: str | None = None, limit: int = 20) -> tuple[str, str]:
    """(title, body) for a project's tracker. Tickets grouped in lifecycle order."""
    from . import tickets as tickets_mod

    name = cfg.get("name") or cfg.get("path") or "project"
    board = tickets_mod.board(cfg["path"], status=status, limit=limit)
    state = board["wip"]

    header = [
        f"**WIP** {state['in_flight']}/{state['limit']} in flight "
        f"({state['slots']} slot(s) free)",
    ]
    tally = " · ".join(
        f"{_status_icon(s)} {s} {n}" for s, n in board["counts"].items() if n
    )
    if tally:
        header.append(tally)

    if not board["tickets"]:
        header.append(
            f"\n*No {status} tickets.*" if status
            else "\n*No tickets yet — `/ideate` to write one.*"
        )
        return f"🗂️ Backlog — {name}", "\n".join(header)

    body = list(header)
    for group, items in board["groups"]:
        body.append(f"\n**{_status_icon(group)} {group}**")
        for ticket in items:
            flag = "" if (ticket.get("acceptance_criteria") or "").strip() else " ⚠️ no criteria"
            money = f"${ticket['spent_usd']:.2f}/${ticket['budget_usd']:.2f}"
            if ticket["budget_exhausted"]:
                money += " ⛔"
            body.append(
                f"`#{ticket['id']}` {str(ticket.get('title') or '')[:70]}"
                f"\n   p{ticket.get('priority', '?')} · {money}{flag}"
                f"{_depends(ticket)}"
            )
    if board["overflow"]:
        body.append(
            f"\n*+{board['overflow']} more — `/backlog` with a status, or the dashboard.*"
        )
    return f"🗂️ Backlog — {name}", "\n".join(body)


ROADMAP_ICONS = {
    "done": "✅", "ready_to_start": "▶️", "in_flight": "🔄", "blocked": "⛔",
}

#: Past this, a Discord embed description is rejected (the hard limit is 4096).
#: `limit` bounds the number of cards; this bounds what long titles can do with
#: them, so the message is always sendable.
ROADMAP_MAX_CHARS = 3900


def _roadmap_state(card: dict) -> str:
    """How a card's state reads on a phone, with what blocks it named."""
    from . import tickets as tickets_mod

    state = card["state"]
    text = (ROADMAP_ICONS.get(state, "•") + " "
            + tickets_mod.ROADMAP_LABELS.get(state, state))
    if state == tickets_mod.ROADMAP_BLOCKED:
        text += " by " + tickets_mod.describe_prerequisites(
            card["unmet_prerequisites"]
        )
    return text


def render_roadmap(cfg: dict, limit: int = 30) -> tuple[str, str]:
    """(title, body) for a project's roadmap — one section per area.

    The same grouping, order and states the dashboard's Roadmap tab draws:
    both read ``tickets.roadmap``, so the two surfaces cannot disagree about
    what is blocked. Bounded like ``/backlog``, because an embed has a size
    whatever the backlog's length.
    """
    from . import tickets as tickets_mod

    name = cfg.get("name") or cfg.get("path") or "project"
    board = tickets_mod.roadmap(cfg["path"], limit=limit)
    title = f"🗺️ Roadmap — {name}"

    tally = " · ".join(
        f"{ROADMAP_ICONS.get(state, '•')} "
        f"{tickets_mod.ROADMAP_LABELS[state]} {board['counts'][state]}"
        for state in tickets_mod.ROADMAP_STATES if board["counts"][state]
    )
    header = [tally] if tally else []

    if not board["cards"]:
        return title, "\n".join(
            header + ["\n*No tickets yet — `/ideate` to write one.*"]
        )

    body = list(header)
    for area, cards in board["groups"]:
        body.append(f"\n**📁 {area}**")
        for card in cards:
            body.append(
                f"`#{card['id']}` {card['title'][:60]}"
                f"\n   {card['status']} · {_roadmap_state(card)}"
            )
    if board["overflow"]:
        body.append(
            f"\n*+{board['overflow']} more ticket(s) — the dashboard's Roadmap "
            f"tab has the rest.*"
        )
    text = "\n".join(body)
    if len(text) > ROADMAP_MAX_CHARS:
        text = (text[:ROADMAP_MAX_CHARS].rsplit("\n", 1)[0]
                + "\n*… truncated — more on the dashboard.*")
    return title, text


def area_reply(ticket_id: int, area: str, actor: str) -> str:
    """Put a ticket in an area from Discord. Returns what to tell the channel."""
    from . import tickets as tickets_mod

    try:
        stored = tickets_mod.set_area(ticket_id, area, actor=actor)
    except tickets_mod.TransitionRefused as exc:
        return f"❌ {exc}"
    return (
        f"📁 `#{ticket_id}` is now in **{stored}**." if stored
        else f"📁 `#{ticket_id}` has no area — it shows under "
             f"**{tickets_mod.UNASSIGNED}** on the roadmap."
    )


def render_ticket(ticket_id: int) -> tuple[str, str] | None:
    """(title, body) for one ticket, or None if there is no such ticket."""
    from . import review as review_mod
    from . import tickets as tickets_mod
    from . import tracker

    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        return None

    status = ticket.get("status") or tickets_mod.BACKLOG
    body = [
        f"{_status_icon(status)} **{status}** · p{ticket.get('priority', '?')} · "
        f"{_money(ticket)} · 📁 {tickets_mod.area_of(ticket)}"
    ]

    prereqs = tickets_mod.prerequisites(ticket)
    if prereqs:
        unmet = tickets_mod.unmet_prerequisites(ticket)
        body.append(
            "\n**Depends on**" + ("  ⛔ blocked" if unmet else "  ✅ all finished")
        )
        for prereq in prereqs:
            icon = "⛔" if prereq.get("status") not in tickets_mod.MET else "✅"
            body.append(
                f"  • {icon} `#{prereq['id']}` {str(prereq.get('title') or '')[:60]}"
                f" · {prereq.get('status') or ''}"
            )

    description = str(ticket.get("description") or "").strip()
    if description and description != str(ticket.get("title") or "").strip():
        body.append(f"\n{description[:900]}")

    criteria = str(ticket.get("acceptance_criteria") or "").strip()
    body.append(
        f"\n**Acceptance criteria**\n{criteria[:900]}" if criteria
        else "\n⚠️ **No acceptance criteria** — the lifecycle will refuse to make "
             "this ticket `ready`."
    )

    verdict = review_mod.latest_verdict(ticket_id)
    if verdict:
        icon = {review_mod.APPROVE: "✅", review_mod.CHANGES_REQUESTED: "🔁"}.get(
            verdict["verdict"], "⚠️"
        )
        body.append(f"\n**Review** {icon} {verdict['verdict']} — {(verdict['reasons'] or '')[:300]}")

    runs = tracker.get_ticket_runs(ticket_id)
    if runs:
        body.append("\n**Runs**")
        for run in runs[-3:]:
            branch = run.get("git_branch") or ""
            body.append(
                f"  • #{run['id']} {run.get('status', '?')}"
                + (f" · `{branch}`" if branch else "")
            )

    history = tickets_mod.events(ticket_id)
    if history:
        body.append("\n**History**")
        for event in history[-5:]:
            move = (
                f"{event.get('from_status')} → {event.get('to_status')}"
                if event.get("from_status") != event.get("to_status") else "note"
            )
            note = (event.get("note") or "").strip()
            body.append(
                f"  • {move} by {event.get('actor', '?')}"
                + (f" — {note[:120]}" if note else "")
            )

    return f"🎫 #{ticket_id} {str(ticket.get('title') or '')[:200]}", "\n".join(body)


# ── Bot ─────────────────────────────────────────────────────────────────────────
class EngCrewBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="eng-crew")

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        print(f"[discord_bot] Logged in as {self.user} (id: {self.user.id})")
        print("[discord_bot] Slash commands synced. Ready.")
        print("[discord_bot] @mention or DM to talk. Name a project + a directive to build.")

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mention = self.user in message.mentions
        if not is_dm and not is_mention:
            return

        content = message.content
        for m in message.mentions:
            content = content.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        content = content.strip()

        # Ideation mode: route everything to the grounded manager.
        cid = message.channel.id
        if cid in _ideate_mode and cid in _ideate_project:
            if not content:
                await message.reply("Describe an idea, or use `/task` for direct dispatch / `/endideate` to exit.")
                return
            await _handle_ideate(message, content)
            return

        projects = load_projects()
        if not content:
            await message.reply(
                "Ask me anything, or name a project and describe a task to build, "
                "e.g. `resolvemind add a logout button`. Projects: "
                + (", ".join(f"`{k}`" for k in projects) or "none configured")
                + "\nOr use `/task` to dispatch, `/backlog` to see a project's tickets."
            )
            return

        # Detect a project alias appearing as a word in the message.
        lowered = content.lower()
        matched = next(
            (k for k in projects if k.lower() in lowered.split()
             or projects[k].get("name", "").lower() in lowered),
            None,
        )
        # A question is a conversation, even when it names a project: "how does
        # resolvemind handle auth?" asks about the code, it does not order a
        # change. Building requires a named project AND a directive.
        from .discuss import is_question

        if is_question(content) or not matched:
            await _handle_discuss(message, content, projects.get(matched) if matched else None)
            return

        # Strip the alias token from the task text.
        task = " ".join(w for w in content.split() if w.lower() != matched.lower()).strip()
        if not task:
            await message.reply(f"What should I do on **{projects[matched]['name']}**?")
            return
        await _post_confirm(message, projects, matched, task)


bot = EngCrewBot()


@bot.tree.command(name="task", description="Dispatch a task to the eng-crew engineering team")
@app_commands.describe(project="Project alias (see /projects)", task="What to build or fix")
async def cmd_task(interaction: discord.Interaction, project: str, task: str):
    projects = load_projects()
    if project not in projects:
        await interaction.response.send_message(_unknown_project(project, projects), ephemeral=True)
        return
    cfg = projects[project]
    embed = discord.Embed(
        title=f"📋 Task — {cfg.get('name', project)}",
        description=f"**Task:** {task}\n\n**Project:** `{cfg['path']}`",
        color=discord.Color.blue(),
    )
    embed.set_footer(text="Files a ticket and builds it on an isolated ai-team/* branch, with review and a budget.")
    await interaction.response.send_message(embed=embed, view=ConfirmView(cfg, task))


@bot.tree.command(name="backlog", description="Show a project's ticket tracker")
@app_commands.describe(
    project="Project alias (see /projects)",
    status="Only tickets in this status (backlog, ready, in_progress, ...)",
)
async def cmd_backlog(interaction: discord.Interaction, project: str, status: str | None = None):
    from . import tickets as tickets_mod

    projects = load_projects()
    if project not in projects:
        await interaction.response.send_message(_unknown_project(project, projects), ephemeral=True)
        return
    if status and status not in tickets_mod.STATUSES:
        await interaction.response.send_message(
            f"❓ Unknown status `{status}`. One of: "
            + ", ".join(f"`{s}`" for s in tickets_mod.STATUSES),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    try:
        title, body = await loop.run_in_executor(
            bot.executor, render_backlog, projects[project], status
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        return
    await interaction.followup.send(embed=discord.Embed(
        title=title[:256], description=body[:4000], color=discord.Color.blurple(),
    ))


@cmd_backlog.autocomplete("status")
async def _backlog_status_autocomplete(interaction: discord.Interaction, current: str):
    from . import tickets as tickets_mod

    return [
        app_commands.Choice(name=s, value=s)
        for s in tickets_mod.STATUSES if current.lower() in s
    ][:25]


@bot.tree.command(name="ticket", description="Show one ticket in full")
@app_commands.describe(id="Ticket ID (see /backlog)")
async def cmd_ticket(interaction: discord.Interaction, id: int):
    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    try:
        view = await loop.run_in_executor(bot.executor, render_ticket, id)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        return
    if not view:
        await interaction.followup.send(f"❓ No ticket #{id}.")
        return
    title, body = view
    await interaction.followup.send(embed=discord.Embed(
        title=title[:256], description=body[:4000], color=discord.Color.blurple(),
    ))
@bot.tree.command(name="roadmap", description="Show a project's roadmap, grouped by area")
@app_commands.describe(project="Project alias (see /projects)")
async def cmd_roadmap(interaction: discord.Interaction, project: str):
    projects = load_projects()
    if project not in projects:
        await interaction.response.send_message(_unknown_project(project, projects), ephemeral=True)
        return

    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    try:
        title, body = await loop.run_in_executor(
            bot.executor, render_roadmap, projects[project]
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        return
    await interaction.followup.send(embed=discord.Embed(
        title=title[:256], description=body[:4000], color=discord.Color.blurple(),
    ))


@bot.tree.command(name="area", description="Put a ticket in an area (its roadmap column)")
@app_commands.describe(
    ticket_id="Ticket ID (see /backlog)",
    area="Short area name, e.g. Engine. Leave empty to clear it.",
)
async def cmd_area(interaction: discord.Interaction, ticket_id: int, area: str = ""):
    await interaction.response.defer()
    loop = asyncio.get_running_loop()
    actor = str(getattr(interaction.user, "name", None) or interaction.user)
    try:
        message = await loop.run_in_executor(
            bot.executor, area_reply, ticket_id, area, actor
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        return
    await interaction.followup.send(message[:2000])


@cmd_area.autocomplete("area")
async def _area_autocomplete(interaction: discord.Interaction, current: str):
    """The areas a configured project already uses — reusing one is the point."""
    from . import tracker

    names: list[str] = []
    try:
        for cfg in load_projects().values():
            for ticket in tracker.list_project_tickets(cfg.get("path") or ""):
                name = (ticket.get("area") or "").strip()
                if name and name not in names and current.lower() in name.lower():
                    names.append(name)
    except Exception:
        return []
    return [app_commands.Choice(name=n, value=n) for n in sorted(names)][:25]


@bot.tree.command(name="queue", description="Show a project's ticket queue")
@app_commands.describe(project="Project alias (see /projects)")
async def cmd_queue(interaction: discord.Interaction, project: str):
    projects = load_projects()
    if project not in projects:
        await interaction.response.send_message(_unknown_project(project, projects), ephemeral=True)
        return
    loop = asyncio.get_running_loop()
    await interaction.response.defer()
    try:
        lines = await loop.run_in_executor(bot.executor, queue_lines, projects[project])
    except Exception as e:
        await interaction.followup.send(f"❌ {e}")
        return
    body = "\n".join(lines)
    await interaction.followup.send(body[:2000])
    for chunk in [body[i:i + 1900] for i in range(2000, len(body), 1900)]:
        await interaction.followup.send(chunk)


@bot.tree.command(name="build", description="Build a ticket that is already in the queue")
@app_commands.describe(ticket_id="Ticket ID (see /queue)")
async def cmd_build(interaction: discord.Interaction, ticket_id: int):
    loop = asyncio.get_running_loop()
    await interaction.response.defer()
    actor = str(getattr(interaction.user, "name", None) or interaction.user)
    try:
        message = await loop.run_in_executor(bot.executor, build_reply, ticket_id, actor)
    except Exception as e:
        await interaction.followup.send(f"❌ {e}")
        return
    await interaction.followup.send(message[:2000])


@bot.tree.command(name="status", description="Show recent and active eng-crew runs")
async def cmd_status(interaction: discord.Interaction):
    from . import tracker
    try:
        active = tracker.get_active_runs()
        recent = tracker.get_recent_runs(limit=8)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        return

    lines = []
    if active:
        lines.append("**Active:**")
        for r in active:
            lines.append(f"  🔄 #{r['id']} · {(r.get('task_text') or '')[:60]}")
    if recent:
        lines.append("\n**Recent:**")
        for r in recent:
            icon = {
                "completed": "✅", "unverified": "⚠️", "failed": "❌",
            }.get(r.get("status"), "•")
            lines.append(f"  {icon} #{r['id']} · {(r.get('task_text') or '')[:60]}")
    await interaction.response.send_message("\n".join(lines) or "No runs yet.")


@bot.tree.command(name="projects", description="List configured project aliases")
async def cmd_projects(interaction: discord.Interaction):
    projects = load_projects()
    if not projects:
        await interaction.response.send_message(
            "No projects configured. Register one on the dashboard, or add aliases to "
            f"`{bot_config_path()}`.", ephemeral=True
        )
        return
    lines = [f"`{k}` — {v.get('name', k)}\n    `{v['path']}`" for k, v in projects.items()]
    await interaction.response.send_message("**Projects:**\n" + "\n".join(lines))


@bot.tree.command(name="ideate", description="Start an ideation session with the AI manager on a project")
@app_commands.describe(project="Project alias (see /projects)")
async def cmd_ideate(interaction: discord.Interaction, project: str):
    projects = load_projects()
    if project not in projects:
        await interaction.response.send_message(_unknown_project(project, projects), ephemeral=True)
        return
    cfg = projects[project]
    cid = interaction.channel_id
    _ideate_project[cid] = cfg
    _ideate_history[cid] = []
    _ideate_mode.add(cid)
    await interaction.response.send_message(
        f"💡 **Ideation mode on {cfg.get('name', project)}.** Talk through an idea — I'll ground it "
        f"in the real code, ask a question or two, and write it up as a ticket for the backlog. "
        f"Approving that ticket (moving it to **ready**) is what starts a build.\n"
        f"`/task` still works for direct dispatch · `/endideate` to exit."
    )


@bot.tree.command(name="newtopic", description="Forget this channel's conversation and start fresh")
async def cmd_newtopic(interaction: discord.Interaction):
    had = len(_chat_history.pop(interaction.channel_id, []))
    if had:
        await interaction.response.send_message(f"Forgot {had // 2} exchange(s). Fresh start.")
    else:
        await interaction.response.send_message("Nothing to forget — this channel has no conversation yet.")


@bot.tree.command(name="endideate", description="Exit ideation mode in this channel")
async def cmd_endideate(interaction: discord.Interaction):
    cid = interaction.channel_id
    _ideate_mode.discard(cid)
    history = _ideate_history.pop(cid, None)
    cfg = _ideate_project.pop(cid, None)
    # Distill this session into the project's persistent vision memory (background).
    if history and cfg:
        loop = asyncio.get_running_loop()

        def _remember():
            from . import manager
            manager.remember(cfg["path"], history, cfg.get("name", ""))

        loop.run_in_executor(bot.executor, _remember)
        await interaction.response.send_message(
            "Exited ideation mode — I'll remember what we discussed. Back to direct dispatch."
        )
    else:
        await interaction.response.send_message("Exited ideation mode. Back to direct dispatch.")


def main() -> None:
    from . import discord_service

    problems = discord_service.preflight()
    if problems:
        raise SystemExit("Cannot start the Discord bot:\n  - " + "\n  - ".join(problems))
    token = os.environ["DISCORD_BOT_TOKEN"].strip()
    # Two failures account for nearly every "the bot won't come online": a token
    # that no longer matches the app, and an intent the app was never granted.
    try:
        bot.run(token)
    except discord.LoginFailure:
        raise SystemExit(discord_service.BAD_TOKEN) from None
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(discord_service.INTENTS_REFUSED) from None


if __name__ == "__main__":
    main()
