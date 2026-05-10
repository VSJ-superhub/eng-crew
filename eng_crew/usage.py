from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal

# Pricing per million tokens (USD). Updated for Claude 4.x family (2026).
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write": 3.75,
    },
    "claude-haiku-4-5": {
        "input": 0.80,
        "output": 4.00,
        "cache_read": 0.08,
        "cache_write": 1.00,
    },
}
_DEFAULT_PRICING = _PRICING["claude-sonnet-4-6"]

AgentRole = Literal["architect", "critic", "coder", "reviewer", "executor", "orchestrator"]


@dataclass
class AgentUsage:
    agent: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self) -> float:
        p = _pricing_for(self.model)
        return (
            self.input_tokens * p["input"]
            + self.output_tokens * p["output"]
            + self.cache_read_tokens * p["cache_read"]
            + self.cache_write_tokens * p["cache_write"]
        ) / 1_000_000

    def as_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
        }


def _pricing_for(model: str) -> dict[str, float]:
    """Return pricing dict for the given model string, falling back to Sonnet rates."""
    for key, price in _PRICING.items():
        if key in model or model in key:
            return price
    return _DEFAULT_PRICING


class UsageTracker:
    """Thread-safe per-agent token and cost accumulator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[AgentUsage] = []

    def record(
        self,
        agent: str,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Add token counts for the given agent+model pair (creates entry if new)."""
        with self._lock:
            for r in self._records:
                if r.agent == agent and r.model == model:
                    r.input_tokens += input_tokens
                    r.output_tokens += output_tokens
                    r.cache_read_tokens += cache_read_tokens
                    r.cache_write_tokens += cache_write_tokens
                    return
            self._records.append(
                AgentUsage(
                    agent=agent,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
            )

    def total_cost_usd(self) -> float:
        with self._lock:
            return sum(r.cost_usd for r in self._records)

    def total_tokens(self) -> int:
        with self._lock:
            return sum(r.total_tokens for r in self._records)

    def budget_remaining(self, budget_usd: float) -> float:
        return max(0.0, budget_usd - self.total_cost_usd())

    def is_over_budget(self, budget_usd: float) -> bool:
        return self.total_cost_usd() >= budget_usd

    def summary(self) -> list[dict[str, object]]:
        with self._lock:
            return [r.as_dict() for r in self._records]

    def reset(self) -> None:
        with self._lock:
            self._records.clear()

    def get(self, agent: str, model: str) -> AgentUsage | None:
        with self._lock:
            for r in self._records:
                if r.agent == agent and r.model == model:
                    return r
            return None


# Module-level singleton — import and use directly in agent nodes
tracker = UsageTracker()
