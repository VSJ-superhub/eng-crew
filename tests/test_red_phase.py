"""Tests for the red-phase check.

These drive real git repositories and real pytest subprocesses: the check
exists to make a claim about code that no longer exists in the tree, and a
mocked git would not test the part that can actually be wrong.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from eng_crew import git_skill, verify
from eng_crew.verify import FAILED, PASSED, SKIPPED, new_test_files, verify_red

TAUTOLOGY = "def test_x():\n    assert True\n"


def _repo() -> Path:
    """A git repo with one committed test, pytest-detectable."""
    root = Path(tempfile.mkdtemp(prefix="engcrew_red_"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_existing.py").write_text(TAUTOLOGY, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(root.parent / ".eng-crew-worktrees" / root.name, ignore_errors=True)


# --- change detection ---------------------------------------------------


def test_changed_paths_sees_untracked_and_modified_but_not_deleted():
    root = _repo()
    try:
        _write(root, "tests/test_new.py", TAUTOLOGY)
        _write(root, "app.py", "x = 1\n")
        (root / "tests" / "test_existing.py").unlink()
        changed = git_skill.changed_paths(root)
        assert "tests/test_new.py" in changed
        assert "app.py" in changed
        assert "tests/test_existing.py" not in changed
    finally:
        _cleanup(root)


def test_new_test_files_keeps_tests_and_conftest_only():
    root = _repo()
    try:
        _write(root, "tests/test_new.py", TAUTOLOGY)
        _write(root, "tests/conftest.py", "\n")
        _write(root, "app.py", "x = 1\n")
        assert new_test_files(root) == ["tests/conftest.py", "tests/test_new.py"]
    finally:
        _cleanup(root)


def test_new_test_files_on_a_non_repo_is_empty_not_an_error():
    root = Path(tempfile.mkdtemp(prefix="engcrew_nogit_"))
    try:
        assert new_test_files(root) == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --- the check itself ---------------------------------------------------


def test_test_that_needs_the_change_is_red():
    """The honest case: the test imports code the run created."""
    root = _repo()
    try:
        _write(root, "app.py", "def compute():\n    return 2\n")
        _write(
            root,
            "tests/test_feature.py",
            "from app import compute\n\n\ndef test_compute():\n    assert compute() == 2\n",
        )
        result = verify_red(root, timeout=120)
        assert result.status == PASSED, result.output
    finally:
        _cleanup(root)


def test_test_that_passes_without_the_change_is_not_red():
    """The failure this check exists for: a test that proves nothing."""
    root = _repo()
    try:
        _write(root, "app.py", "def compute():\n    return 2\n")
        _write(root, "tests/test_feature.py", "def test_compute():\n    assert 1 + 1 == 2\n")
        result = verify_red(root, timeout=120)
        assert result.status == FAILED
        assert "tests/test_feature.py" in result.output
    finally:
        _cleanup(root)


def test_modified_test_file_is_replayed_against_the_old_code():
    root = _repo()
    try:
        _write(root, "app.py", "def compute():\n    return 2\n")
        _write(
            root,
            "tests/test_existing.py",
            TAUTOLOGY + "\n\ndef test_new_behaviour():\n    from app import compute\n"
            "    assert compute() == 2\n",
        )
        result = verify_red(root, timeout=120)
        assert result.status == PASSED, result.output
    finally:
        _cleanup(root)


def test_run_with_no_new_tests_is_skipped():
    root = _repo()
    try:
        _write(root, "app.py", "x = 1\n")
        assert verify_red(root, timeout=120).status == SKIPPED
    finally:
        _cleanup(root)


def test_conftest_alone_is_not_a_new_test():
    root = _repo()
    try:
        _write(root, "tests/conftest.py", "# fixtures\n")
        assert verify_red(root, timeout=120).status == SKIPPED
    finally:
        _cleanup(root)


def test_non_pytest_project_is_skipped():
    root = Path(tempfile.mkdtemp(prefix="engcrew_nopy_"))
    try:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        assert verify_red(root, timeout=120).status == SKIPPED
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_leaves_no_worktree_behind():
    root = _repo()
    try:
        _write(root, "app.py", "def compute():\n    return 2\n")
        _write(root, "tests/test_feature.py", "from app import compute\n\n\ndef test_c():\n    assert compute() == 2\n")
        before = git_skill.list_worktrees(root)
        verify_red(root, timeout=120)
        after = git_skill.list_worktrees(root)
        assert after == before
        assert not any("redphase" in w["path"] for w in after)
    finally:
        _cleanup(root)
