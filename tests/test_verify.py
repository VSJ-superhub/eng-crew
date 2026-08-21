"""Tests for the deterministic verification gate.

Written without fixtures so they run under a bare runner as well as pytest.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from eng_crew import verify
from eng_crew.verify import (
    FAILED,
    PASSED,
    SKIPPED,
    Check,
    CheckResult,
    VerificationResult,
    detect_checks,
    run_check,
)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="engcrew_verify_"))


# --- detection ----------------------------------------------------------


def test_detects_pytest_from_tests_dir():
    root = _tmp()
    (root / "tests").mkdir()
    names = [c.name for c in detect_checks(root)]
    assert "pytest" in names


def test_detects_pytest_from_pyproject_mentioning_pytest():
    root = _tmp()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    assert [c.name for c in detect_checks(root)] == ["pytest"]


def test_pyproject_without_pytest_is_not_a_test_project():
    root = _tmp()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert detect_checks(root) == []


def test_detects_npm_test_and_build():
    root = _tmp()
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "build": "tsc"}}), encoding="utf-8"
    )
    names = [c.name for c in detect_checks(root)]
    assert "npm test" in names and "npm run build" in names


def test_ignores_npm_init_placeholder_test_script():
    root = _tmp()
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
        encoding="utf-8",
    )
    assert detect_checks(root) == []


def test_malformed_package_json_does_not_raise():
    root = _tmp()
    (root / "package.json").write_text("{not json", encoding="utf-8")
    assert detect_checks(root) == []


def test_detects_cargo_and_go():
    root = _tmp()
    (root / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (root / "go.mod").write_text("module x\n", encoding="utf-8")
    names = [c.name for c in detect_checks(root)]
    assert "cargo test" in names and "go test" in names


def test_empty_project_has_no_checks():
    assert detect_checks(_tmp()) == []


def test_prefers_project_venv_interpreter_over_the_running_one():
    root = _tmp()
    (root / "tests").mkdir()
    # Lay down both layouts; whichever this OS uses must win over sys.executable.
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "bin").mkdir(parents=True)
    win = root / ".venv" / "Scripts" / "python.exe"
    posix = root / ".venv" / "bin" / "python"
    win.write_text("", encoding="utf-8")
    posix.write_text("", encoding="utf-8")
    chosen = verify.python_for(root)
    assert chosen in (str(win), str(posix))
    assert detect_checks(root)[0].cmd[0] == chosen


def test_falls_back_to_running_interpreter_without_a_venv():
    root = _tmp()
    (root / "tests").mkdir()
    assert verify.python_for(root) == sys.executable


# --- gate semantics -----------------------------------------------------


def test_no_checks_is_unverified_but_not_a_failure():
    result = verify.verify(_tmp())
    assert result.passed is True
    assert result.unverified is True
    assert "UNVERIFIED" in result.summary()


def test_truncated_output_fails_even_with_no_checks():
    result = verify.verify(_tmp(), agent_output="[TRUNCATED: max turns reached] partial work")
    assert result.passed is False
    assert result.truncated is True
    assert "truncated" in result.summary().lower()


def test_truncation_marker_matches_the_provider_constant():
    # The gate keys off the exact marker claude_cli writes; keep them in lockstep.
    from eng_crew.providers import claude_cli  # noqa: F401
    src = Path("eng_crew/providers/claude_cli.py").read_text(encoding="utf-8")
    assert verify.TRUNCATION_MARKER in src


def test_failing_check_fails_the_gate_and_reports_output():
    failing = CheckResult("pytest", "test", FAILED, "E   assert 1 == 2", "-m pytest")
    result = VerificationResult(results=[failing])
    assert result.passed is False
    assert result.ran_any is True
    assert "pytest" in result.summary()
    assert "assert 1 == 2" in result.failure_report()


def test_skipped_checks_do_not_fail_the_gate():
    skipped = CheckResult("cargo test", "test", SKIPPED, "toolchain not installed", "cargo test")
    result = VerificationResult(results=[skipped])
    assert result.passed is True
    assert result.unverified is True  # nothing actually ran


def test_passing_check_passes_and_is_not_unverified():
    result = VerificationResult(results=[CheckResult("pytest", "test", PASSED, "", "-m pytest")])
    assert result.passed is True
    assert result.unverified is False
    assert "PASSED" in result.summary()


def test_failure_report_is_bounded():
    huge = CheckResult("pytest", "test", FAILED, "x" * 50_000, "-m pytest")
    assert len(VerificationResult(results=[huge]).failure_report(max_chars=500)) <= 500


# --- real subprocess execution -----------------------------------------


def test_run_check_reports_nonzero_exit_as_failure():
    root = _tmp()
    check = Check("boom", [sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(1)"], "test")
    res = run_check(check, root, timeout=30)
    assert res.status == FAILED
    assert "nope" in res.output


def test_run_check_reports_zero_exit_as_pass():
    check = Check("fine", [sys.executable, "-c", "print('ok')"], "test")
    res = run_check(check, _tmp(), timeout=30)
    assert res.status == PASSED


def test_run_check_skips_missing_toolchain():
    check = Check("ghost", ["definitely-not-a-real-binary-xyz", "--version"], "test")
    assert run_check(check, _tmp(), timeout=30).status == SKIPPED


def test_run_check_times_out_as_failure():
    check = Check("sleepy", [sys.executable, "-c", "import time; time.sleep(5)"], "test")
    res = run_check(check, _tmp(), timeout=1)
    assert res.status == FAILED
    assert "timed out" in res.output
