"""
preprocessor.py — local-first context reduction before LLM calls.

Two gates, each silently skipped on any error:
  ast_extract     — strip function bodies, keep signatures (tree-sitter via Rust)
  entropy_select  — knapsack-optimal fragment selection within a token budget (Rust)

Both require entropy_engine to be installed. Falls back gracefully when absent.
"""
from __future__ import annotations

import math
import os
import re
import sys
from collections import Counter


_FILE_BLOCK_RE = re.compile(
    r"^(=== .+? ===)\n(.*?)(?=^=== |\Z)",
    re.MULTILINE | re.DOTALL,
)

_EXT_TO_LANG = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
}


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _split_fragments(text: str) -> list[str]:
    """Split on === ... === block boundaries, falling back to double-newline."""
    parts = re.split(r"(?=^=== )", text, flags=re.MULTILINE)
    if len(parts) > 1:
        return [p for p in parts if p.strip()]
    return [p.strip() for p in text.split("\n\n") if p.strip()]


# ── Gate 1: AST signature extraction ─────────────────────────────────────────

def ast_extract(file_contents: str) -> str:
    """Strip function bodies, keep signatures for Python/TS/Rust blocks.

    Reduces file content by ~60-70% for supported languages.
    Passes through unchanged for unsupported extensions or on any error.
    """
    before = len(file_contents)
    try:
        from entropy_engine import batch_extract_signatures  # type: ignore
    except ImportError:
        return file_contents

    blocks = list(_FILE_BLOCK_RE.finditer(file_contents))
    if not blocks:
        return file_contents

    items: list[tuple[str, str, str]] = []
    meta: list[tuple[str, str, str | None]] = []

    for m in blocks:
        header = m.group(1)
        body = m.group(2)
        # extract path from "=== path/to/file.ext ===" or "=== FILE: path ===" headers
        path_match = re.search(r"FILE:\s*(.+?)\s*===|===\s*(.+?)\s*===", header)
        path = (path_match.group(1) or path_match.group(2) or "").strip() if path_match else ""
        ext = os.path.splitext(path)[1].lower()
        lang = _EXT_TO_LANG.get(ext)
        meta.append((header, body, lang))
        if lang and path:
            items.append((path, body, lang))

    if not items:
        return file_contents

    try:
        extracted_map = {p: c for p, c in batch_extract_signatures(items) if c.strip()}
    except Exception:
        return file_contents

    parts = []
    for m, (header, body, lang) in zip(blocks, meta):
        path_match = re.search(r"FILE:\s*(.+?)\s*===|===\s*(.+?)\s*===", header)
        path = (path_match.group(1) or path_match.group(2) or "").strip() if path_match else ""
        if lang and path in extracted_map:
            body = extracted_map[path]
        parts.append(f"{header}\n{body}")

    result = "\n".join(parts)
    if before != len(result):
        print(
            f"[preprocessor] ast_extract: {before:,} -> {len(result):,} chars",
            file=sys.stderr,
        )
    return result


# ── Gate 2: Entropy-optimal fragment selection ────────────────────────────────

def entropy_select(text: str, token_budget: int = 3000) -> str:
    """Select the highest-entropy fragments that fit within token_budget.

    Uses Rust knapsack when entropy_engine is installed; falls back to a
    greedy pure-Python sort ranked by Shannon entropy density.
    """
    # Fast exact check — skip if already within budget
    try:
        from entropy_engine import count_tokens as _ct  # type: ignore
        if _ct(text) <= token_budget:
            return text
    except ImportError:
        if len(text) <= token_budget * 4:
            return text

    before = len(text)
    fragments = _split_fragments(text)
    if not fragments:
        return text[: token_budget * 4]

    # Rust knapsack path
    try:
        from entropy_engine import count_tokens_batch, select_optimal  # type: ignore

        token_counts = [max(1, t) for t in count_tokens_batch(fragments)]
        selected = select_optimal(fragments, token_counts, token_budget)
        result = "\n\n".join(selected)
        print(
            f"[preprocessor] entropy_select: {before:,} -> {len(result):,} chars "
            f"({len(selected)}/{len(fragments)} fragments) [Rust]",
            file=sys.stderr,
        )
        return result
    except ImportError:
        pass

    # Pure-Python greedy fallback
    char_budget = token_budget * 4
    scored = sorted(fragments, key=_shannon_entropy, reverse=True)
    result_parts: list[str] = []
    used = 0
    for frag in scored:
        if used + len(frag) <= char_budget:
            result_parts.append(frag)
            used += len(frag)
        if used >= char_budget:
            break

    result = "\n\n".join(result_parts) if result_parts else text[:char_budget]
    print(
        f"[preprocessor] entropy_select: {before:,} -> {len(result):,} chars "
        f"({len(result_parts)}/{len(fragments)} fragments) [Python fallback]",
        file=sys.stderr,
    )
    return result
