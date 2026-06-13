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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ENG_CREW_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Stack selection ---
    stack: str = "quality"

    # --- Global Defaults (used if agent-specific ones are missing) ---
    provider: Optional[str] = Field(default=None, alias="ENG_CREW_PROVIDER")
    default_provider: str = "anthropic"
    default_model: str = "claude-sonnet-4-6"

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

    # --- Executor ---
    executor_provider: Optional[str] = None
    executor_model: Optional[str] = None

    # --- Simple Executor (short-circuit path for simple tasks) ---
    simple_executor_provider: Optional[str] = None
    simple_executor_model: Optional[str] = None

    # --- General ---
    budget_usd: float = 5.0
    max_tokens: int = 8192
    data_dir: Path = Path(".eng-crew")

    # --- Rust entropy engine (auto-on when entropy_engine is installed) ---
    entropy_engine_enabled: bool = Field(default_factory=lambda: _entropy_engine_available())

    # --- Dashboard ---
    dashboard_port: int = 9000
    dashboard_host: str = "127.0.0.1"

    # --- Pipeline ---
    branch_prefix: str = "ai-team"
    require_approval: bool = True

    @field_validator("data_dir", mode="before")
    @classmethod
    def resolve_data_dir(cls, v: object) -> Path:
        return Path(str(v)).expanduser().resolve()

    def get_agent_config(self, role: str) -> Dict[str, str]:
        """Return {provider, model} for a given agent role."""
        # 1. Check agent-specific settings in env/config
        p = getattr(self, f"{role}_provider", None)
        m = getattr(self, f"{role}_model", None)

        if p and m:
            return {"provider": p, "model": m}

        # 2. Check active stack
        from .stacks import get_stack_config
        stack_cfg = get_stack_config(self.stack)
        if role in stack_cfg:
            return {
                "provider": p or stack_cfg[role]["provider"],
                "model":    m or stack_cfg[role]["model"]
            }

        # 3. Use global provider override (ENG_CREW_PROVIDER) if set, else default_provider
        effective_provider = self.provider or self.default_provider

        # 4. Fallback to defaults
        return {
            "provider": p or effective_provider,
            "model":    m or self.default_model
        }

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def load_settings(env_file: str | Path | None = None) -> Settings:
    if env_file is not None:
        return Settings(_env_file=str(env_file))
    return Settings()


settings: Settings = load_settings()

# Module-level constants the tracker and dashboard routers import directly.
DATA_DIR = settings.data_dir
DB_PATH = settings.data_dir / "tracking.db"
CLAUDE_WEEKLY_BUDGET: float = float(os.environ.get("ENG_CREW_WEEKLY_BUDGET", 0.0))
