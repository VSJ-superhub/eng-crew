from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from eng_crew.config import load_settings

app = typer.Typer(
    name="eng-crew",
    help="Autonomous AI engineering team — decomposes, codes, reviews, and executes software tasks.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def run(
    task: str = typer.Argument(..., help="Natural language description of the task to execute."),
    project_path: Path = typer.Argument(
        ..., help="Path to the project root.", exists=True, file_okay=False, resolve_path=True
    ),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
    no_approval: bool = typer.Option(False, "--no-approval", help="Skip human-in-the-loop approval step."),
    budget: Optional[float] = typer.Option(None, "--budget", help="Override budget in USD."),
    log_path: Optional[Path] = typer.Option(
        None, "--log-path",
        help="Where this run's output is being captured; recorded on the run so "
             "the dashboard can serve it.",
    ),
    base: Optional[str] = typer.Option(
        None, "--base",
        help="Branch (or tag/SHA) to base this run's branch on, instead of the "
             "project checkout's current HEAD. Use it to stack a run on top of "
             "an earlier run's branch.",
    ),
) -> None:
    """Run a task on a project using the full multi-agent pipeline."""
    cfg = load_settings(env_file)
    if budget is not None:
        cfg = cfg.model_copy(update={"budget_usd": budget})
    if no_approval:
        cfg = cfg.model_copy(update={"require_approval": False})

    console.print(Panel(f"[bold green]Task:[/] {task}\n[bold blue]Project:[/] {project_path}", title="eng-crew run"))

    if base:
        from eng_crew import git_skill

        if not git_skill.branch_exists(project_path, base):
            console.print(f"[red]No such branch in {project_path}: {base}[/red]")
            raise typer.Exit(1)
        console.print(f"[bold blue]Base:[/] {base}")

    if cfg.require_approval:
        dashboard_url = f"http://{cfg.dashboard_host}:{cfg.dashboard_port}"
        console.print(f"\n[bold yellow]ℹ️  This run requires approval.[/]")
        console.print(f"[dim]Start the dashboard in another terminal:[/]")
        console.print(f"  [cyan]eng-crew dashboard[/] (or [cyan]eng-crew dashboard --port {cfg.dashboard_port}[/])")
        console.print(f"[dim]Then visit:[/] [bold cyan]{dashboard_url}[/]\n")

    try:
        from eng_crew.pipeline import run_pipeline  # type: ignore[import]

        run_pipeline(
            task=task,
            project_path=project_path,
            settings=cfg,
            log_path=str(log_path) if log_path else None,
            base_branch=base,
        )
    except ImportError:
        console.print("[red]Pipeline modules not yet installed. Run `pip install eng-crew[full]`.[/red]")
        raise typer.Exit(1)


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind the dashboard server."),
    port: int = typer.Option(0, "--port", "-p", help="Port for the dashboard (0 = use config value)."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """Start the eng-crew dashboard web server."""
    cfg = load_settings(env_file)
    effective_port = port if port != 0 else cfg.dashboard_port
    effective_host = host if host != "127.0.0.1" else cfg.dashboard_host

    console.print(f"[bold]Starting dashboard at http://{effective_host}:{effective_port}[/]")

    try:
        import uvicorn  # type: ignore[import]
        from eng_crew.dashboard import create_app  # type: ignore[import]

        uvicorn.run(create_app(cfg), host=effective_host, port=effective_port)
    except ImportError as exc:
        console.print(f"[red]Dashboard dependencies missing: {exc}[/red]")
        raise typer.Exit(1)


@app.command()
def discord(
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
    check: bool = typer.Option(
        False, "--check", help="Only report whether the bot could start, then exit."
    ),
) -> None:
    """Run the Discord bot in the foreground, or check why it will not start.

    Started as a background service the bot's output goes to a log; run it here
    and whatever stops it — a missing extra, a rejected token, an intent the
    Discord app was never granted — is printed straight at you.
    """
    from eng_crew.config import load_env_into_environ
    from eng_crew import discord_service

    load_env_into_environ(env_file)
    problems = discord_service.preflight()
    if problems:
        for problem in problems:
            console.print(f"[red]x[/red] {problem}")
        tail = discord_service.log_tail()
        if tail:
            console.print(Panel(tail, title=f"last output — {discord_service.log_path()}"))
        raise typer.Exit(1)
    if check:
        console.print("[green]ok[/green] token set and discord.py installed — "
                      "run `eng-crew discord` to start the bot.")
        return

    from eng_crew.discord_bot import main as run_bot

    console.print("[bold]Starting the eng-crew Discord bot[/] (Ctrl-C to stop)")
    run_bot()


@app.command()
def project(
    project_path: Optional[Path] = typer.Argument(None, help="Project path to inspect or register."),
    list_all: bool = typer.Option(False, "--list", "-l", help="List all registered projects."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """Manage projects — register, inspect, or list tracked projects."""
    cfg = load_settings(env_file)

    if list_all or project_path is None:
        _list_projects(cfg)
        return

    resolved = project_path.resolve()
    if not resolved.exists():
        console.print(f"[red]Path does not exist: {resolved}[/red]")
        raise typer.Exit(1)

    table = Table(title=f"Project: {resolved.name}", show_header=False)
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")
    table.add_row("Path", str(resolved))
    table.add_row("Data dir", str(cfg.data_dir))
    table.add_row("Provider", cfg.default_provider)
    table.add_row("Branch prefix", cfg.branch_prefix)
    console.print(table)


@app.command()
def plans(
    project_path: Path = typer.Argument(
        ..., help="Path to the project root.", exists=True, file_okay=False, resolve_path=True
    ),
    list_plans: bool = typer.Option(False, "--list", "-l", help="List existing run plans."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """List the architect plans produced by this project's runs."""
    cfg = load_settings(env_file)

    try:
        from eng_crew.sprint import get_sprint_plans  # type: ignore[import]

        plans = get_sprint_plans(project_path=project_path, settings=cfg)
    except ImportError:
        console.print("[yellow]Plan module not available — run `pip install eng-crew[full]`.[/yellow]")
        raise typer.Exit(1)

    if not plans:
        console.print("[dim]No run plans found for this project.[/dim]")
        return

    table = Table(title=f"Run plans: {project_path.name}")
    table.add_column("ID", style="bold cyan")
    table.add_column("Title")
    table.add_column("Status")
    for plan in plans:
        table.add_row(str(plan.get("id", "")), str(plan.get("title", "")), str(plan.get("status", "")))
    console.print(table)


@app.command()
def queue(
    project_path: Path = typer.Argument(
        ..., help="Path to the project root.", exists=True, file_okay=False, resolve_path=True
    ),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """Show a project's ticket queue and how much work is in flight."""
    cfg = load_settings(env_file)
    from eng_crew import budget as budget_mod
    from eng_crew import tickets, tracker

    path = str(project_path)
    state = tickets.wip(path, cfg)
    console.print(
        f"[bold]WIP[/] {state['in_flight']}/{state['limit']} in flight "
        f"({state['slots']} slot(s) free)"
    )

    rows = tracker.list_project_tickets(path)
    if not rows:
        console.print("[dim]No tickets for this project.[/dim]")
        return

    table = Table(title=f"Tickets: {project_path.name}")
    table.add_column("ID", style="bold cyan")
    table.add_column("Pri")
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("AC", justify="center")
    table.add_column("Spend")
    table.add_column("Depends on")
    for t in rows:
        spend = budget_mod.annotate(t, cfg)
        money = f"${spend['spent_usd']:.2f}/${spend['budget_usd']:.2f}"
        prereqs = tickets.prerequisites(t)
        unmet = tickets.unmet_prerequisites(t)
        deps = ", ".join(f"#{p['id']}" for p in prereqs)
        if unmet:
            deps = f"[red]blocked by {tickets.describe_prerequisites(unmet)}[/red]"
        table.add_row(
            str(t["id"]), str(t.get("priority", "")), str(t.get("status", "")),
            str(t.get("title", ""))[:60],
            "yes" if (t.get("acceptance_criteria") or "").strip() else "[red]no[/red]",
            f"[red]{money}[/red]" if spend["budget_exhausted"] else money,
            deps,
        )
    console.print(table)


@app.command()
def budget(
    ticket_id: int = typer.Argument(..., help="Ticket ID (see `eng-crew queue`)."),
    amount: Optional[float] = typer.Argument(
        None, help="New budget in USD. Omit to just show what the ticket has spent."
    ),
    actor: str = typer.Option("user", "--actor", help="Who is authorising the change."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """Show or change a ticket's spend authority.

    Raising it is what releases a ticket whose work was stopped for budget; the
    next build picks up the new figure. This starts no run.
    """
    if amount is not None and amount < 0:
        console.print("[red]a budget cannot be negative[/red]")
        raise typer.Exit(1)
    cfg = load_settings(env_file)
    from eng_crew import budget as budget_mod

    state = (
        budget_mod.set_budget(ticket_id, amount, actor, settings=cfg)
        if amount is not None
        else budget_mod.status(ticket_id, cfg)
    )
    if not state:
        console.print(f"[red]no ticket {ticket_id}[/red]")
        raise typer.Exit(1)
    colour = "red" if state["exhausted"] else "green"
    console.print(
        f"ticket {ticket_id}: [{colour}]${state['spent_usd']:.2f}[/] spent of "
        f"${state['budget_usd']:.2f} (${state['remaining_usd']:.2f} left)"
    )


@app.command()
def ticket(
    ticket_id: int = typer.Argument(..., help="Ticket ID to build (see `eng-crew queue`)."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
    no_approval: bool = typer.Option(False, "--no-approval", help="Skip human-in-the-loop approval step."),
    log_path: Optional[Path] = typer.Option(
        None, "--log-path", help="Where this run's output is being captured."
    ),
    base: Optional[str] = typer.Option(
        None, "--base",
        help="Branch (or tag/SHA) to base this ticket's run on, instead of the "
             "project checkout's current HEAD.",
    ),
) -> None:
    """Build one ticket — the ticket's own text, not a placeholder."""
    cfg = load_settings(env_file)
    if no_approval:
        cfg = cfg.model_copy(update={"require_approval": False})

    from eng_crew import review as _review
    from eng_crew import tickets as _tickets
    from eng_crew import tracker as _tracker
    from eng_crew.run import run_ticket

    try:
        run_ticket(
            ticket_id,
            actor="cli",
            log_path=str(log_path) if log_path else None,
            base_branch=base,
            settings=cfg,
        )
    except _tickets.TransitionRefused as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    # What the independent reviewer said, and where that left the ticket.
    verdict = _review.latest_verdict(ticket_id)
    status = (_tracker.get_backlog_item(ticket_id) or {}).get("status", "?")
    if verdict:
        colour = "green" if verdict["verdict"] == _review.APPROVE else "yellow"
        console.print(f"[{colour}]review: {verdict['verdict']}[/] — {verdict['reasons']}")
    console.print(f"ticket {ticket_id} is now [bold]{status}[/]")


@app.command()
def resume(
    run_id: int = typer.Argument(..., help="Run ID to resume (shown in dashboard or terminal output)."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """Resume an interrupted run from its last checkpoint."""
    cfg = load_settings(env_file)
    console.print(f"[bold]Resuming run {run_id}...[/]")
    try:
        from eng_crew.run import resume_run  # type: ignore[import]

        ok = resume_run(run_id)
        if not ok:
            console.print(f"[red]Run {run_id} not found or could not be resumed.[/red]")
            raise typer.Exit(1)
    except ImportError:
        console.print("[red]Pipeline modules not available. Run `pip install eng-crew`.[/red]")
        raise typer.Exit(1)


@app.command()
def status(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of recent runs to show."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """Show recent runs and their status."""
    cfg = load_settings(env_file)
    try:
        from eng_crew.tracker import list_runs  # type: ignore[import]

        runs = list_runs(limit=limit)
    except ImportError:
        console.print("[red]Tracker module not available.[/red]")
        raise typer.Exit(1)

    if not runs:
        console.print("[dim]No runs found.[/dim]")
        return

    table = Table(title="Recent runs")
    table.add_column("ID", style="bold cyan")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Project")
    table.add_column("Started")
    for r in runs:
        status_color = {
            "completed": "green", "unverified": "yellow",
            "failed": "red", "running": "cyan",
        }.get(
            str(r.get("status", "")), "white"
        )
        table.add_row(
            str(r.get("id", "")),
            f"[{status_color}]{r.get('status', '')}[/{status_color}]",
            str(r.get("task_text", ""))[:60],
            str(r.get("project_path", ""))[-30:],
            str(r.get("started_at", ""))[:19],
        )
    console.print(table)


def _list_projects(cfg: object) -> None:
    try:
        from eng_crew.registry import list_projects  # type: ignore[import]

        projects = list_projects(cfg)
    except ImportError:
        console.print("[yellow]Registry module not available.[/yellow]")
        return

    if not projects:
        console.print("[dim]No projects registered.[/dim]")
        return

    table = Table(title="Registered projects")
    table.add_column("Name", style="bold")
    table.add_column("Path")
    for p in projects:
        table.add_row(p.get("name", ""), p.get("path", ""))
    console.print(table)


@app.command()
def worktrees(
    project_path: Path = typer.Argument(Path("."), help="Project to inspect."),
) -> None:
    """List run worktrees and whether each is safe to remove."""
    from eng_crew import git_skill

    if not git_skill.is_git_repo(project_path):
        console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    root = git_skill.repo_root(project_path)
    entries = [w for w in git_skill.list_worktrees(root)
               if Path(w["path"]).resolve() != root.resolve()]
    if not entries:
        console.print("No run worktrees.")
        return

    for w in entries:
        st = git_skill.worktree_status(root, w["path"])
        if st["dirty"]:
            state, colour = "uncommitted changes", "yellow"
        elif st["unmerged"]:
            state, colour = f"{st['unmerged']} unmerged commit(s)", "yellow"
        else:
            state, colour = "clean", "green"
        console.print(
            f"[{colour}]{state:24}[/{colour}] {st['age_days']:5.1f}d  "
            f"{w.get('branch', '?')}" + chr(10) + f"    {w['path']}"
        )


@app.command()
def prune_worktrees(
    project_path: Path = typer.Argument(Path("."), help="Project to prune."),
    keep_last: int = typer.Option(5, "--keep", help="Always keep this many newest."),
    days: float = typer.Option(7.0, "--days", help="Only remove worktrees older than this."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed."),
) -> None:
    """Remove old worktrees that are clean and fully merged.

    Worktrees with uncommitted changes or unmerged commits are never removed —
    on the single-agent tier nothing commits, so those changes are the output of
    a run. Remove one of those yourself once you have dealt with it.
    """
    from eng_crew import git_skill

    if not git_skill.is_git_repo(project_path):
        console.print("[red]Not a git repository.[/red]")
        raise typer.Exit(1)

    handled = git_skill.prune_worktrees(
        project_path, keep_last=keep_last, max_age_days=days, dry_run=dry_run
    )
    if not handled:
        console.print("Nothing to prune.")
        return
    verb = "Would remove" if dry_run else "Removed"
    for path in handled:
        console.print(f"{verb}: {path}")
    console.print(f"[green]{verb} {len(handled)} worktree(s).[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
