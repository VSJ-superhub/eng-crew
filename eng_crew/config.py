from __future__ import annotations
import os
from pathlib import Path
from typing import Literal, Optional, Dict

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _entropy_engine_available() -> bool:
    try:
        import entropy_engine  # noqa: F401
        return True
    except ImportError:
        return False


_PKG_DIR = Path(__file__).resolve().parent              # .../eng_crew
_ENG_CREW_ROOT = _PKG_DIR.parent                        # a checkout root, or site-packages


def is_source_checkout(root: Path = _ENG_CREW_ROOT) -> bool:
    """True when eng_crew is being run out of its own repository.

    Installed from PyPI, ``root`` is ``site-packages``: no place for a database
    (pip deletes it on upgrade) and a worse place for an API key. A checkout has
    the project files next to the package, and whatever ``.env`` and
    ``.eng-crew/`` the developer has already been using — those keep working
    exactly as they did.
    """
    if not (root / "eng_crew").is_dir():
        return False
    return (root / "pyproject.toml").exists() or (root / ".git").exists()


def user_dir() -> Path:
    """Where an installed eng-crew keeps its config and data: ``~/.eng-crew``."""
    override = os.environ.get("ENG_CREW_HOME")
    return Path(override).expanduser() if override else Path.home() / ".eng-crew"


def env_files(root: Path = _ENG_CREW_ROOT, cwd: Path | None = None) -> list[Path]:
    """The .env files to read, lowest precedence first.

    A checkout reads its own, and only its own — the MCP server is launched by
    an editor from whatever project is open, so resolving against the process
    cwd there silently pointed it at a stale tracking.db. An installed eng-crew
    has no "own" directory, so it reads ``~/.eng-crew/.env`` and lets a ``.env``
    in the directory you are working in override it.
    """
    if is_source_checkout(root):
        return [root / ".env"]
    return [user_dir() / ".env", (cwd or Path.cwd()) / ".env"]


def default_data_dir(root: Path = _ENG_CREW_ROOT) -> Path:
    """Where runs, logs and tracking.db live when ENG_CREW_DATA_DIR says nothing."""
    if is_source_checkout(root):
        return root / ".eng-crew"
    return user_dir()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENG_CREW_",
        env_file=[str(p) for p in env_files()],
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Stack selection ---
    stack: str = "quality"

    # --- Global Defaults (used if agent-specific ones are missing) ---
    provider: Optional[str] = Field(default=None, alias="ENG_CREW_PROVIDER")
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-5"

    # --- API keys ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")

    # --- Orchestrator ---
    orchestrator_provider: Optional[str] = None
    orchestrator_model: Optional[str] = None

    # --- Architect ---
    architect_provider: Optional[str] = None
    architect_model: Optional[str] = None

    # --- Coder ---
    coder_provider: Optional[str] = None
    coder_model: Optional[str] = None

    # --- Reviewer ---
    reviewer_provider: Optional[str] = None
    reviewer_model: Optional[str] = None

    # --- Ticket reviewer ---
    # The independent review of a landed run against its ticket's acceptance
    # criteria (eng_crew/review.py): one cheap call, and never a CLI provider —
    # a reviewer that can edit the code it is judging is not a reviewer.
    ticket_reviewer_provider: Optional[str] = None
    ticket_reviewer_model: Optional[str] = None
    ticket_review_enabled: bool = True

    # --- Executor ---
    executor_provider: Optional[str] = None
    executor_model: Optional[str] = None

    # --- Simple Executor (short-circuit path for simple tasks) ---
    simple_executor_provider: Optional[str] = None
    simple_executor_model: Optional[str] = None

    # --- Single Agent (middle tier for medium tasks: one capable call) ---
    single_agent_provider: Optional[str] = None
    single_agent_model: Optional[str] = None

    # --- Worktree isolation ---
    # Each run gets its own git worktree and branch, so it never stashes the
    # developer's changes or moves the branch they are working on.
    worktree_isolation: bool = True
    worktree_dir: str = ""                       # default: sibling of the repo
    worktree_link_dirs: str = ".venv,venv,node_modules"
    # Old worktrees are pruned at the start of a run. Only ones that are clean
    # and fully merged are ever removed — an uncommitted worktree is a run's
    # output, so it is kept until you deal with it.
    # A paused run holds its thread and worktree, so it is abandoned after
    # this long rather than waiting forever. 0 disables the limit.
    pause_timeout_seconds: float = 3600.0
    pause_poll_seconds: float = 1.0

    # Commit whatever a run produced, in the worktree, after verification.
    # Without this the single-agent tier's output is only uncommitted changes.
    commit_run_output: bool = True
    worktree_auto_prune: bool = True
    worktree_keep_last: int = 5
    worktree_retention_days: float = 7.0
    # Once a run finishes, sweep worktrees that are clean and fully merged
    # regardless of age — including this run's own, when it produced nothing
    # worth keeping. Age-based retention alone lets a week of runs pile up.
    worktree_prune_on_finish: bool = True

    # --- Builder turn budget ---
    # How many agent turns the single-agent builder gets before it is cut off.
    # Hitting the cap truncates the work mid-change and hands it to a repair
    # pass. Test-first work spends turns writing and running failing tests before
    # any implementation starts, so it gets more room.
    builder_max_turns: int = 60
    builder_test_first_max_turns: int = 100

    # --- MCP servers given to agents ---
    # Every agent CLI call runs with an explicit MCP configuration and ignores
    # the user's global and project servers, so an agent reaches only what is
    # named here: inline JSON ({"mcpServers": {...}}) or a path to such a file.
    # Empty by default, and eng-crew's own server is always excluded — a run may
    # not dispatch, plan or build other eng-crew work (eng_crew/mcp_policy.py).
    agent_mcp_servers: str = ""

    # --- Verification gate ---
    # Runs the project's own tests/build after the agent tiers and decides the
    # run's final status. Disable only for a project with no runnable checks.
    verification_enabled: bool = True
    verification_max_fix_attempts: int = 1
    verification_timeout: int = 300
    # Repair passes may not quietly rewrite a test to make the gate go green.
    # Test files named in the failure output stay editable — fixing a broken
    # new test is legitimate. "strict" fails the run, "warn" only reports it.
    verification_test_lock: str = "strict"  # strict | warn | off
    # Replay the run's new tests against pre-change code; they must fail there,
    # or they do not demonstrate the change works. Strict by default: the
    # builder is told to work test-first when a task carries acceptance
    # criteria, so "the new tests failed before the change" is a promise the
    # gate should hold it to, not a note in the log. A run that adds no tests
    # skips this rather than failing it. "warn" only reports, "off" disables.
    verification_red_phase: str = "strict"  # strict | warn | off

    # --- Ticket queue ---
    # There are no sprints: tickets form one continuous queue, and this is the
    # only thing bounding how much work is in flight at once. A ticket cannot
    # enter in_progress while this many are already in_progress/in_review/in_qa
    # for the project.
    wip_limit: int = 3

    # --- Ticket spend authority ---
    # What a ticket is authorised to spend across all of its runs — builds,
    # repair passes, and reviews together. Work proceeds within it; reaching it
    # stops the next paid step and escalates to a person, rather than carrying
    # on spending. Overridable per ticket (backlog_items.budget_usd).
    ticket_budget_usd: float = 15.0

    # --- General ---
    budget_usd: float = 5.0
    max_tokens: int = 8192
    data_dir: Path = Field(default_factory=default_data_dir)

    # --- Rust entropy engine (auto-on when entropy_engine is installed) ---
    entropy_engine_enabled: bool = Field(default_factory=lambda: _entropy_engine_available())

    # --- RAG (LSH file search per subtask) ---
    rag_enabled: bool = True
    rag_top_k: int = 6          # max extra files surfaced per subtask

    # --- Context token budget for entropy_select ---
    token_budget: int = 3000    # tokens; BPE-accurate when entropy_engine installed

    # --- Dashboard ---
    dashboard_port: int = 9000
    dashboard_host: str = "127.0.0.1"

    # --- Pipeline ---
    branch_prefix: str = "ai-team"
    require_approval: bool = True

    # Full multi-agent decomposition (architect -> critic -> HITL -> specialist
    # coders -> reviewer -> executor) is an explicit opt-in escape hatch. By
    # default every task runs on the single-agent tier: one capable agentic CLI
    # call that plans, codes, and tests in a single context. Multi-agent
    # orchestration adds token cost, latency, and lossy handoffs between agents;
    # it rarely beats a single strong agent on real coding tasks. Set this to
    # true (ENG_CREW_ENABLE_MULTI_AGENT=true) only when you specifically want the
    # decomposition graph for large, parallelizable work.
    enable_multi_agent: bool = False

    @field_validator("data_dir", mode="before")
    @classmethod
    def resolve_data_dir(cls, v: object) -> Path:
        p = Path(str(v)).expanduser()
        if not p.is_absolute():
            # A checkout anchors a relative data_dir to the repo root, as it
            # always has. An installed package must not: that would put the
            # database inside site-packages.
            base = _ENG_CREW_ROOT if is_source_checkout() else Path.cwd()
            p = base / p
        return p.resolve()

    _CLI_ONLY_ROLES: frozenset = frozenset({"executor", "simple_executor", "single_agent"})
    _CLI_PROVIDERS: frozenset = frozenset({"claude_cli", "gemini_cli"})
    _CLI_FALLBACK_PROVIDER: str = "claude_cli"
    _CLI_FALLBACK_MODEL: str = "claude-sonnet-5"

    def get_agent_config(self, role: str) -> Dict[str, str]:
        """Return {provider, model} for a given agent role."""
        # 1. Check agent-specific settings in env/config
        p = getattr(self, f"{role}_provider", None)
        m = getattr(self, f"{role}_model", None)

        if p and m:
            cfg = {"provider": p, "model": m}
        else:
            # 2. Check active stack
            from .stacks import get_stack_config
            stack_cfg = get_stack_config(self.stack)
            if role in stack_cfg:
                cfg = {
                    "provider": p or stack_cfg[role]["provider"],
                    "model":    m or stack_cfg[role]["model"],
                }
            else:
                # 3. Use global provider override (ENG_CREW_PROVIDER) if set, else default_provider
                effective_provider = self.provider or self.default_provider
                cfg = {
                    "provider": p or effective_provider,
                    "model":    m or self.default_model,
                }

        # Executor roles must use a CLI provider — they drive agentic file edits.
        if role in self._CLI_ONLY_ROLES and cfg["provider"] not in self._CLI_PROVIDERS:
            import logging
            logging.getLogger(__name__).warning(
                "Role %r configured with non-CLI provider %r — overriding to %r/%r",
                role, cfg["provider"], self._CLI_FALLBACK_PROVIDER, self._CLI_FALLBACK_MODEL,
            )
            cfg = {"provider": self._CLI_FALLBACK_PROVIDER, "model": self._CLI_FALLBACK_MODEL}

        return cfg

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def load_settings(env_file: str | Path | None = None) -> Settings:
    if env_file is not None:
        return Settings(_env_file=str(env_file))
    return Settings(_env_file=[str(p) for p in env_files()])


def load_env_into_environ(env_file: str | Path | None = None) -> None:
    """Put the configured .env files into os.environ without overriding it.

    Surfaces that read ``os.environ`` directly — the providers, the dashboard's
    intake chat, every subprocess a run spawns — need the keys from .env there,
    and they must find them wherever this install keeps its .env.
    """
    paths = [Path(env_file)] if env_file is not None else env_files()
    # env_files() is lowest precedence first; setdefault means first-wins.
    for path in reversed(paths):
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


settings: Settings = load_settings()

# Module-level constants the tracker and dashboard routers import directly.
DATA_DIR = settings.data_dir
DB_PATH = settings.data_dir / "tracking.db"
CLAUDE_WEEKLY_BUDGET: float = float(os.environ.get("ENG_CREW_WEEKLY_BUDGET", 0.0))
