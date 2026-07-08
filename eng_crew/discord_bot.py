"""
eng-crew Discord bot — dispatch tasks to your engineering team from mobile.

Thin, single-agent-first bot:
  /task project:<alias> task:<desc>   Dispatch a task (with a Run/Cancel confirm)
  /status                             Recent and active runs
  /projects                           Configured project aliases
  @mention / DM                       Name a project alias in your message; the
                                      rest is treated as the task.

There is no plan-approval gate: eng-crew's default single-agent tier plans
internally and applies the change on an isolated ai-team/* branch. You review
the diff afterward. (The opt-in multi-agent graph keeps its dashboard HITL gate.)

Run:  python -m eng_crew.discord_bot   (requires DISCORD_BOT_TOKEN in .env)
"""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import discord
from discord import app_commands

BOT_CONFIG_PATH = Path(__file__).parent / "bot_config.json"

# ── Per-channel ideation state ──────────────────────────────────────────────────
# When a channel is in ideation mode, @mention/DM messages are routed to the
# grounded manager instead of being dispatched as a task.
_ideate_project: dict[int, dict] = {}   # channel_id -> project cfg
_ideate_history: dict[int, list] = {}   # channel_id -> [{"role","content"}]
_ideate_mode: set[int] = set()          # channel_ids currently in ideation mode


# ── Project resolution ──────────────────────────────────────────────────────────
def load_projects() -> dict:
    """Merge bot_config.json aliases with DB-registered projects (aliases win)."""
    projects: dict = {}
    if BOT_CONFIG_PATH.exists():
        try:
            projects = json.loads(BOT_CONFIG_PATH.read_text())
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
    except Exception:
        pass
    return projects


# ── Task execution ──────────────────────────────────────────────────────────────
def _dispatch_blocking(task: str, cfg: dict) -> tuple[dict, str, str]:
    """Run one task end-to-end (blocking). Returns (run_detail, summary, branch)."""
    from .config import load_settings
    from .run import run_task
    from . import tracker

    # Single-agent default has no HITL gate; disable approval so nothing blocks.
    settings = load_settings().model_copy(update={"require_approval": False})
    state = run_task(task=task, project_path=cfg["path"], settings=settings)
    run_id = state.get("run_id")
    detail = tracker.get_run_detail(run_id) if run_id else {}
    return detail or {}, state.get("final_summary") or "", state.get("git_branch") or ""


async def _execute(channel: "discord.abc.Messageable", cfg: dict, task: str,
                   loop: asyncio.AbstractEventLoop) -> None:
    status = await channel.send(
        embed=discord.Embed(
            title=f"🔄 {cfg.get('name', 'Project')} — running",
            description=f"**Task:** {task}\n\n⏳ Single-agent is implementing...",
            color=discord.Color.yellow(),
        )
    )

    # Guard: don't start a second run on a project that already has one active.
    try:
        from . import tracker
        active = tracker.get_active_runs_for_project(cfg["path"])
    except Exception:
        active = []
    if active:
        ids = ", ".join(f"#{r['id']}" for r in active)
        await status.edit(embed=discord.Embed(
            title=f"⚠️ {cfg.get('name', 'Project')} — blocked",
            description=f"A run is already active for this project ({ids}). "
                        f"Wait for it to finish before starting another.",
            color=discord.Color.orange(),
        ))
        return

    try:
        detail, summary, branch = await loop.run_in_executor(
            bot.executor, _dispatch_blocking, task, cfg
        )
        run_status = (detail or {}).get("status", "unknown")
        ok = run_status == "completed"
        embed = discord.Embed(
            title=f"{'✅' if ok else '❌'} {cfg.get('name', 'Project')} — {run_status.upper()}",
            description=(summary or "No summary.")[:2000],
            color=discord.Color.green() if ok else discord.Color.red(),
        )
        if branch:
            embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
        cost = (detail or {}).get("total_cost_usd")
        if cost:
            try:
                embed.add_field(name="Cost", value=f"${float(cost):.4f}", inline=True)
            except (TypeError, ValueError):
                pass
        await status.edit(embed=embed)
    except Exception as e:
        await status.edit(embed=discord.Embed(
            title=f"❌ {cfg.get('name', 'Project')} — ERROR",
            description=str(e)[:2000],
            color=discord.Color.red(),
        ))


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
                content="⏳ Dispatching to eng-crew...", view=self
            )
            loop = asyncio.get_running_loop()
            asyncio.create_task(_execute(interaction.channel, self._cfg, self._task, loop))
            self.stop()
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
    embed.set_footer(text="Runs on an isolated ai-team/* branch. Review the diff after.")
    await target.reply(embed=embed, view=ConfirmView(cfg, task))


# ── Ideation mode (grounded manager) ────────────────────────────────────────────

class IdeateBuildView(discord.ui.View):
    """Shown on a manager build proposal — dispatch it or keep ideating."""
    def __init__(self, cfg: dict, task: str):
        super().__init__(timeout=600)
        self._cfg = cfg
        self._task = task

    @discord.ui.button(label="🚀 Build it", style=discord.ButtonStyle.success)
    async def build(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(content="🚀 Dispatching to eng-crew...", view=self)
            loop = asyncio.get_running_loop()
            asyncio.create_task(_execute(interaction.channel, self._cfg, self._task, loop))
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

    if reply.proposal and reply.proposal.get("task"):
        prop = reply.proposal
        desc = prop["task"]
        if prop.get("rationale"):
            desc += f"\n\n*{prop['rationale']}*"
        embed = discord.Embed(title="💡 Ready to build", description=desc, color=discord.Color.purple())
        await message.channel.send(embed=embed, view=IdeateBuildView(cfg, prop["task"]))


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
        print("[discord_bot] Use /task, or @mention me and name a project alias.")

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
                "Name a project and describe the task, e.g. "
                "`resolvemind add a logout button`. Projects: "
                + (", ".join(f"`{k}`" for k in projects) or "none configured")
                + "\nOr use `/task`."
            )
            return

        # Detect a project alias appearing as a word in the message.
        lowered = content.lower()
        matched = next(
            (k for k in projects if k.lower() in lowered.split()
             or projects[k].get("name", "").lower() in lowered),
            None,
        )
        if not matched:
            await message.reply(
                "I couldn't tell which project. Name one of: "
                + (", ".join(f"`{k}`" for k in projects) or "none configured")
                + " — or use `/task project:<alias> task:<desc>`."
            )
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
        keys = ", ".join(f"`{k}`" for k in projects) or "none — edit eng_crew/bot_config.json"
        await interaction.response.send_message(
            f"❓ Unknown project `{project}`. Available: {keys}", ephemeral=True
        )
        return
    cfg = projects[project]
    embed = discord.Embed(
        title=f"📋 Task — {cfg.get('name', project)}",
        description=f"**Task:** {task}\n\n**Project:** `{cfg['path']}`",
        color=discord.Color.blue(),
    )
    embed.set_footer(text="Runs on an isolated ai-team/* branch. Review the diff after.")
    await interaction.response.send_message(embed=embed, view=ConfirmView(cfg, task))


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
            icon = {"completed": "✅", "failed": "❌"}.get(r.get("status"), "•")
            lines.append(f"  {icon} #{r['id']} · {(r.get('task_text') or '')[:60]}")
    await interaction.response.send_message("\n".join(lines) or "No runs yet.")


@bot.tree.command(name="projects", description="List configured project aliases")
async def cmd_projects(interaction: discord.Interaction):
    projects = load_projects()
    if not projects:
        await interaction.response.send_message(
            "No projects configured. Edit `eng_crew/bot_config.json`.", ephemeral=True
        )
        return
    lines = [f"`{k}` — {v.get('name', k)}\n    `{v['path']}`" for k, v in projects.items()]
    await interaction.response.send_message("**Projects:**\n" + "\n".join(lines))


@bot.tree.command(name="ideate", description="Start an ideation session with the AI manager on a project")
@app_commands.describe(project="Project alias (see /projects)")
async def cmd_ideate(interaction: discord.Interaction, project: str):
    projects = load_projects()
    if project not in projects:
        keys = ", ".join(f"`{k}`" for k in projects) or "none — edit eng_crew/bot_config.json"
        await interaction.response.send_message(
            f"❓ Unknown project `{project}`. Available: {keys}", ephemeral=True
        )
        return
    cfg = projects[project]
    cid = interaction.channel_id
    _ideate_project[cid] = cfg
    _ideate_history[cid] = []
    _ideate_mode.add(cid)
    await interaction.response.send_message(
        f"💡 **Ideation mode on {cfg.get('name', project)}.** Talk through an idea — I'll ground it "
        f"in the real code, ask a question or two, and propose something to build.\n"
        f"`/task` still works for direct dispatch · `/endideate` to exit."
    )


@bot.tree.command(name="endideate", description="Exit ideation mode in this channel")
async def cmd_endideate(interaction: discord.Interaction):
    cid = interaction.channel_id
    _ideate_mode.discard(cid)
    _ideate_history.pop(cid, None)
    _ideate_project.pop(cid, None)
    await interaction.response.send_message("Exited ideation mode. Back to direct dispatch.")


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN not set in .env")
    bot.run(token)


if __name__ == "__main__":
    main()
