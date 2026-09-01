"""Tests for the repair-pass test lock.

A repair pass is handed failing tests and told to fix them; the cheap way out
is to rewrite the test. These cover what the lock freezes and what it lets
through, in the same fixture-free style as test_verify.py.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from eng_crew.verify import (
    LOCK_OFF,
    LOCK_STRICT,
    LOCK_WARN,
    is_test_path,
    snapshot_tests,
    lock_violations,
)


# The literal Windows separator, spelled without an escape so the value is
# unambiguous in source.
WIN_SEP = chr(92)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="engcrew_lock_"))


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- path classification ------------------------------------------------


def test_recognises_conventional_test_paths():
    for rel in (
        "tests/test_thing.py",
        "test/test_thing.py",
        "test_thing.py",
        "pkg/thing_test.py",
        "pkg/thing_test.go",
        "src/__tests__/thing.js",
        "src/thing.test.tsx",
        "src/thing.spec.ts",
    ):
        assert is_test_path(rel), rel


def test_does_not_claim_ordinary_source_files():
    for rel in (
        "eng_crew/verify.py",
        "src/latest.py",
        "src/contest.js",
        "docs/testing.md",
        "",
    ):
        assert not is_test_path(rel), rel


def test_windows_separators_are_normalised():
    assert is_test_path(f"tests{WIN_SEP}test_thing.py")


# --- snapshotting -------------------------------------------------------


def test_snapshot_covers_tests_and_ignores_source_and_vendored_dirs():
    root = _tmp()
    _write(root, "tests/test_a.py", "assert True")
    _write(root, "src/app.py", "x = 1")
    _write(root, "node_modules/pkg/tests/test_vendor.py", "assert True")
    _write(root, ".venv/lib/tests/test_dep.py", "assert True")

    snap = snapshot_tests(root)

    assert set(snap) == {"tests/test_a.py"}


def test_snapshot_of_missing_project_is_empty():
    assert snapshot_tests(_tmp() / "nope") == {}


def test_snapshot_hash_tracks_content():
    root = _tmp()
    _write(root, "tests/test_a.py", "assert True")
    before = snapshot_tests(root)
    _write(root, "tests/test_a.py", "assert False")
    assert snapshot_tests(root) != before


# --- violations ---------------------------------------------------------


def test_untouched_tests_are_not_violations():
    snap = {"tests/test_a.py": "aaa"}
    assert lock_violations(snap, dict(snap)) == []


def test_rewriting_an_unimplicated_test_is_a_violation():
    before = {"tests/test_a.py": "aaa"}
    after = {"tests/test_a.py": "bbb"}
    assert lock_violations(before, after, "tests/test_other.py::test_x failed") == [
        "tests/test_a.py"
    ]


def test_deleting_a_test_is_a_violation():
    assert lock_violations({"tests/test_a.py": "aaa"}, {}) == ["tests/test_a.py"]


def test_test_named_in_the_failures_stays_editable():
    before = {"tests/test_a.py": "aaa"}
    after = {"tests/test_a.py": "bbb"}
    failures = "FAILED tests/test_a.py::test_x - AssertionError"
    assert lock_violations(before, after, failures) == []


def test_exemption_matches_native_separators_too():
    before = {"tests/test_a.py": "aaa"}
    after = {"tests/test_a.py": "bbb"}
    assert lock_violations(before, after, f"at tests{WIN_SEP}test_a.py line 3") == []


def test_new_tests_added_during_repair_are_not_violations():
    before = {"tests/test_a.py": "aaa"}
    after = {"tests/test_a.py": "aaa", "tests/test_new.py": "ccc"}
    assert lock_violations(before, after) == []


def test_reports_every_violation_sorted():
    before = {"tests/test_b.py": "b", "tests/test_a.py": "a", "tests/test_c.py": "c"}
    after = {"tests/test_b.py": "x", "tests/test_a.py": "y", "tests/test_c.py": "c"}
    assert lock_violations(before, after) == ["tests/test_a.py", "tests/test_b.py"]


def test_lock_modes_are_distinct():
    assert len({LOCK_STRICT, LOCK_WARN, LOCK_OFF}) == 3
