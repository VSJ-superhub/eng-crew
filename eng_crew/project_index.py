from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

# Optional Rust entropy engine — used for shingle hashing when available
try:
    from entropy_engine import hash_shingles as _rust_hash_shingles  # type: ignore[import]

    _RUST_ENGINE = True
except ImportError:
    _RUST_ENGINE = False

_CODE_EXTS = frozenset(
    {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
        ".swift", ".cpp", ".c", ".h", ".cs", ".rb", ".php", ".sh",
        ".yaml", ".yml", ".toml", ".json", ".md",
    }
)
_IGNORE_DIRS = frozenset(
    {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        "dist", "build", ".eng-crew", ".mypy_cache", ".ruff_cache",
    }
)
_MAX_FILE_BYTES = 128 * 1024
_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_MIN_TOKEN_LEN = 3

# LSH parameters: 128 bits split into 16 bands of 8 rows each
_N_PROJECTIONS = 128
_N_BANDS = 16
_ROWS_PER_BAND = _N_PROJECTIONS // _N_BANDS
_SEED = 42


def _tokenize(text: str) -> Counter[str]:
    tokens = [
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if len(t) >= _MIN_TOKEN_LEN and not t.isdigit()
    ]
    bigrams = [f"{tokens[i]}~{tokens[i + 1]}" for i in range(len(tokens) - 1)]
    return Counter(tokens + bigrams)


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes()[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _walk_code(root: Path):
    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if any(part in _IGNORE_DIRS for part in rel.parts):
            continue
        if p.is_file() and p.suffix.lower() in _CODE_EXTS:
            yield p


class ProjectIndex:
    """LSH-based approximate nearest-neighbor index for code search.

    Uses random projection SimHash with banding for candidate retrieval,
    then cosine similarity over TF-IDF vectors for ranking.
    When entropy_engine is installed, shingle hashing uses the Rust implementation.
    """

    def __init__(self) -> None:
        self._root: Optional[Path] = None
        self._rel_paths: list[str] = []
        self._tfidf: list[dict[str, float]] = []
        self._norms: list[float] = []
        self._idf: dict[str, float] = {}
        # Transposed projection matrix: term -> list of N_PROJECTIONS weights
        self._proj: dict[str, list[float]] = {}
        # LSH bands: list of N_BANDS dicts, each mapping band_bytes -> [file_idx, ...]
        self._bands: list[dict[bytes, list[int]]] = [{} for _ in range(_N_BANDS)]
        self._ready = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index(self, project_path: str | Path) -> int:
        """Index all code files under project_path. Returns number of files indexed."""
        root = Path(project_path).expanduser().resolve()
        self._root = root
        self._ready = False

        raw: list[tuple[str, Counter[str]]] = []
        for p in _walk_code(root):
            tokens = _tokenize(_read_text(p))
            if tokens:
                raw.append((str(p.relative_to(root)), tokens))

        n = len(raw)
        if n == 0:
            return 0

        # IDF: terms appearing in at least min_df documents
        df: Counter[str] = Counter()
        for _, tokens in raw:
            for term in set(tokens):
                df[term] += 1
        min_df = 2 if n >= 5 else 1
        self._idf = {
            t: math.log((n + 1) / (c + 1)) + 1.0
            for t, c in df.items()
            if c >= min_df
        }

        # TF-IDF vectors
        self._rel_paths = []
        self._tfidf = []
        self._norms = []
        for rel_path, tokens in raw:
            total = sum(tokens.values())
            vec: dict[str, float] = {
                term: (cnt / total) * self._idf[term]
                for term, cnt in tokens.items()
                if term in self._idf
            }
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self._rel_paths.append(rel_path)
            self._tfidf.append(vec)
            self._norms.append(norm)

        # Random projection matrix (transposed): term -> [w0, w1, ..., w127]
        rng = random.Random(_SEED)
        self._proj = {
            term: [rng.gauss(0, 1) for _ in range(_N_PROJECTIONS)]
            for term in self._idf
        }

        # Build LSH bands
        self._bands = [defaultdict(list) for _ in range(_N_BANDS)]
        for idx, vec in enumerate(self._tfidf):
            sig = self._signature(vec)
            for b in range(_N_BANDS):
                s = b * _ROWS_PER_BAND
                self._bands[b][sig[s : s + _ROWS_PER_BAND]].append(idx)

        self._ready = True
        return n

    def search(self, query: str, k: int = 5) -> list[tuple[str, float]]:
        """Return top-k (relative_path, cosine_score) for the query string."""
        if not self._ready:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []

        total = sum(tokens.values())
        q_vec: dict[str, float] = {
            term: (cnt / total) * self._idf[term]
            for term, cnt in tokens.items()
            if term in self._idf
        }
        if not q_vec:
            return []

        q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0
        q_sig = self._signature(q_vec)

        # Candidate retrieval via band matches
        candidates: set[int] = set()
        for b in range(_N_BANDS):
            s = b * _ROWS_PER_BAND
            for idx in self._bands[b].get(q_sig[s : s + _ROWS_PER_BAND], []):
                candidates.add(idx)

        # Fall back to full scan when no band matches
        if not candidates:
            candidates = set(range(len(self._tfidf)))

        # Rank by cosine similarity
        scored = [
            (
                self._rel_paths[idx],
                sum(q_vec.get(t, 0.0) * v for t, v in self._tfidf[idx].items())
                / (q_norm * self._norms[idx]),
            )
            for idx in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    @property
    def file_count(self) -> int:
        return len(self._rel_paths)

    @property
    def root(self) -> Optional[Path]:
        return self._root

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _signature(self, tfidf: dict[str, float]) -> bytes:
        dots = [0.0] * _N_PROJECTIONS
        for term, weight in tfidf.items():
            pw = self._proj.get(term)
            if pw is not None:
                for i in range(_N_PROJECTIONS):
                    dots[i] += weight * pw[i]
        return bytes(1 if d >= 0 else 0 for d in dots)
