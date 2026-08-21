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
) -> None:
    """Run a task on a project using the full multi-agent pipeline."""
    cfg = load_settings(env_file)
    if budget is not None:
        cfg = cfg.model_copy(update={"budget_usd": budget})
    if no_approval:
        cfg = cfg.model_copy(update={"require_approval": False})

    console.print(Panel(f"[bold green]Task:[/] {task}\n[bold blue]Project:[/] {project_path}", title="eng-crew run"))

    if cfg.require_approval:
        dashboard_url = f"http://{cfg.dashboard_host}:{cfg.dashboard_port}"
        console.print(f"\n[bold yellow]ℹ️  This run requires approval.[/]")
        console.print(f"[dim]Start the dashboard in another terminal:[/]")
        console.print(f"  [cyan]eng-crew dashboard[/] (or [cyan]eng-crew dashboard --port {cfg.dashboard_port}[/])")
        console.print(f"[dim]Then visit:[/] [bold cyan]{dashboard_url}[/]\n")

    try:
        from eng_crew.pipeline import run_pipeline  # type: ignore[import]

        run_pipeline(task=task, project_path=project_path, settings=cfg)
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
def sprint(
    project_path: Path = typer.Argument(
        ..., help="Path to the project root.", exists=True, file_okay=False, resolve_path=True
    ),
    list_plans: bool = typer.Option(False, "--list", "-l", help="List existing sprint plans."),
    env_file: Optional[Path] = typer.Option(None, "--env", help="Path to .env file."),
) -> None:
    """View and manage sprint plans for a project."""
    cfg = load_settings(env_file)

    try:
        from eng_crew.sprint import get_sprint_plans  # type: ignore[import]

        plans = get_sprint_plans(project_path=project_path, settings=cfg)
    except ImportError:
        console.print("[yellow]Sprint module not available — run `pip install eng-crew[full]`.[/yellow]")
        raise typer.Exit(1)

    if not plans:
        console.print("[dim]No sprint plans found for this project.[/dim]")
        return

    table = Table(title=f"Sprint plans: {project_path.name}")
    table.add_column("ID", style="bold cyan")
    table.add_column("Title")
    table.add_column("Status")
    for plan in plans:
        table.add_row(str(plan.get("id", "")), str(plan.get("title", "")), str(plan.get("status", "")))
    console.print(table)


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
        status_color = {"completed": "green", "failed": "red", "running": "yellow"}.get(
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
