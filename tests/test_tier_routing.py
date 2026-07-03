"""Tests for complexity-tier model downgrade routing.

These guard the `downgrade_for_tier` map itself (its (provider, model, tier)
behaviour), which is unchanged by the single-agent tier.

Routing note: as of the single-agent middle tier, "medium" tasks route to
`single_execute` (one capable CLI call), "complex" tasks take the full pipeline
that reaches the specialist coder, and "simple" tasks short-circuit to the
simple_executor. The downgrade map below is retained as a defensive no-op.
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


def test_classifier_bands_after_retune():
    """Trivial -> simple, ordinary feature/fix -> medium (single-agent tier),
    broad/architectural -> complex. Absence of file paths must NOT push a real
    task down to 'simple' (the dispatch convention omits file paths)."""
    # Trivial: a small-edit verb on a short prompt.
    assert classify("fix typo") == "simple"
    assert classify("bump the version number") == "simple"
    assert classify("rename the getUser function to fetchUser") == "simple"

    # Ordinary feature/fix with no file paths -> medium, not simple.
    assert classify("add pagination to the runs table, 20 per page, preserve filters") == "medium"
    assert classify("add a CSV export endpoint for runs and wire it into the dashboard") == "medium"
    assert classify("the footer link points to the wrong url") == "medium"

    # Broad / architectural -> complex.
    assert classify(
        "refactor the auth system and migrate the pipeline to a new workflow architecture"
    ) == "complex"


def test_downgrade_map_behaviour():
    """The downgrade map acts only on 'medium' pairs; 'simple' passes through."""
    # A short task classifies 'simple' and would bypass the coder entirely.
    assert classify("fix typo") == "simple"
    assert downgrade_for_tier("anthropic", "claude-sonnet-4-6", "simple") == \
        "claude-sonnet-4-6"  # no effect

    # The map still rewrites the 'medium' pair (retained as a defensive no-op
    # now that 'medium' routes to the single-agent tier, not the coder).
    assert downgrade_for_tier("anthropic", "claude-sonnet-4-6", "medium") != \
        "claude-sonnet-4-6"
