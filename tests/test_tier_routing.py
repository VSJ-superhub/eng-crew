"""Tests for complexity-tier model downgrade routing.

Regression guard: the downgrade must fire on the tier where the specialist coder
actually runs. Simple tasks short-circuit to the simple_executor and never reach
the coder, so the downgrade targets the "medium" tier, not "simple".
"""

from eng_crew.stacks import downgrade_for_tier
from eng_crew.agents.complexity_classifier import classify


def test_medium_tier_downgrades_sonnet_to_haiku():
    assert downgrade_for_tier("anthropic", "claude-sonnet-4-6", "medium") == \
        "claude-haiku-4-5-20251001"


def test_medium_tier_downgrades_known_pairs():
    assert downgrade_for_tier("deepseek", "deepseek-reasoner", "medium") == "deepseek-chat"
    assert downgrade_for_tier("gemini", "gemini-2.5-pro", "medium") == "gemini-2.0-flash"


def test_complex_tier_keeps_top_model():
    # Complex tasks justify the strongest model — no downgrade.
    assert downgrade_for_tier("anthropic", "claude-sonnet-4-6", "complex") == \
        "claude-sonnet-4-6"


def test_simple_tier_is_noop_for_coder():
    # Simple tasks never reach the coder; the coder default tier passes through.
    assert downgrade_for_tier("anthropic", "claude-sonnet-4-6", "simple") == \
        "claude-sonnet-4-6"


def test_unknown_pair_passes_through():
    assert downgrade_for_tier("ollama", "qwen2.5-coder:32b", "medium") == \
        "qwen2.5-coder:32b"


def test_reachability_invariant():
    """The tier that downgrades must be a tier that routes through the full
    pipeline (i.e. NOT 'simple', which short-circuits past the coder)."""
    # A short task classifies 'simple' and would bypass the coder entirely.
    assert classify("fix typo") == "simple"
    assert downgrade_for_tier("anthropic", "claude-sonnet-4-6", "simple") == \
        "claude-sonnet-4-6"  # no effect, as expected — coder never sees this tier

    # The downgrade only acts on 'medium', which DOES run the coder.
    assert downgrade_for_tier("anthropic", "claude-sonnet-4-6", "medium") != \
        "claude-sonnet-4-6"
