"""Tests for the deterministic verification gate.

Written without fixtures so they run under a bare runner as well as pytest.
"""
from __future__ import annotations

import json
import os
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


# --- launching shimmed commands (npm & friends on Windows) --------------
#
# `npm` on Windows is `npm.cmd`; CreateProcess cannot start it by its bare
# name, so a bare ["npm", ...] raised [WinError 2] and every JS/TS run came
# back unverified.


def _shim(root: Path, name: str, message: str) -> Path:
    """A tiny executable on PATH named like a toolchain, .cmd on Windows."""
    if os.name == "nt":
        path = root / f"{name}.cmd"
        path.write_text("@echo off\r\necho " + message, encoding="utf-8")
    else:
        path = root / name
        path.write_text("#!/bin/sh\necho " + message, encoding="utf-8")
        path.chmod(0o755)
    return path


def test_run_check_launches_a_shimmed_command_by_its_resolved_path():
    """A .cmd/.bat shim is resolved through PATHEXT and actually executed."""
    bin_dir = _tmp()
    _shim(bin_dir, "npmshim", "shim-ran")

    launched: list[list[str]] = []
    real_run = verify.subprocess.run

    def spy(cmd, *a, **kw):
        launched.append(list(cmd))
        return real_run(cmd, *a, **kw)

    old_path = os.environ["PATH"]
    verify.subprocess.run = spy
    os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
    try:
        res = run_check(Check("npmshim", ["npmshim", "run", "lint"], "lint"), _tmp(), timeout=30)
    finally:
        verify.subprocess.run = real_run
        os.environ["PATH"] = old_path

    # Launched by the path shutil.which resolved, not the bare name, and
    # without a shell.
    assert launched, "the check was never launched"
    assert Path(launched[-1][0]).is_file()
    assert launched[-1][1:] == ["run", "lint"]
    assert res.status == PASSED
    assert "shim-ran" in res.output


def test_run_check_fails_when_an_available_command_cannot_be_launched():
    """A positive probe followed by an OSError is a failure, never a skip."""
    boom = OSError(2, "[WinError 2] The system cannot find the file specified")
    real_run = verify.subprocess.run
    real_which = verify.shutil.which

    def explode(cmd, *a, **kw):
        raise boom

    verify.subprocess.run = explode
    verify.shutil.which = lambda exe, *a, **kw: f"/usr/bin/{exe}"
    try:
        res = run_check(Check("npm test", ["npm", "test", "--silent"], "test"), _tmp(), timeout=30)
    finally:
        verify.subprocess.run = real_run
        verify.shutil.which = real_which

    assert res.status == FAILED
    assert "WinError 2" in res.output


def test_run_check_still_skips_a_toolchain_that_is_not_installed():
    """Nothing on PATH is still a quiet skip, as before."""
    check = Check("ghostly", ["eng-crew-no-such-tool-zzz", "--version"], "test")
    res = run_check(check, _tmp(), timeout=30)
    assert res.status == SKIPPED
    assert "not installed" in res.output


# --- false completion ---------------------------------------------------
#
# Regression guards for run 100240, which was truncated at max turns, had its
# single repair pass die on a provider error, and still landed in the runs
# table as "completed".


def test_verify_accepts_an_established_truncation():
    """A re-verify that ignores agent output can still be told it was truncated."""
    d = _tmp()  # no checks detectable -> unverified territory
    result = verify.verify(str(d), agent_output="", truncated=True)
    assert result.truncated is True
    assert result.passed is False, "a truncated implementation must never pass"


def test_untruncated_empty_project_is_unverified_not_failed():
    d = _tmp()
    result = verify.verify(str(d), agent_output="")
    assert result.passed is True
    assert result.unverified is True


def test_truncation_outranks_unverified():
    """With nothing to run, truncation is the only evidence — it must win."""
    r = VerificationResult(results=[], truncated=True)
    assert r.passed is False
    assert r.unverified is False


def test_passing_checks_clear_a_truncation_only_when_they_ran():
    ran_ok = VerificationResult(results=[CheckResult(name="pytest", kind="test", status=PASSED)], truncated=True)
    assert ran_ok.ran_any is True
    assert ran_ok.passed is False, "sticky until something vouches for the tree"

    nothing_ran = VerificationResult(results=[CheckResult(name="pytest", kind="test", status=SKIPPED)], truncated=True)
    assert nothing_ran.ran_any is False


def test_pipeline_status_requires_an_explicit_pass():
    """None means the gate never reported; only True is success."""
    def status_for(verified):
        return "completed" if verified is True else "failed"

    assert status_for(True) == "completed"
    assert status_for(False) == "failed"
    assert status_for(None) == "failed", "run 100239 reported completed on a 529"


# --- linters and type checkers ------------------------------------------


def _lint_names(root: Path) -> list[str]:
    return [c.name for c in detect_checks(root) if c.kind == verify.LINT]


def test_detects_ruff_and_mypy_configured_in_pyproject():
    root = _tmp()
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n\n[tool.ruff]\nline-length = 100\n"
        "\n[tool.mypy]\nstrict = true\n",
        encoding="utf-8",
    )
    assert _lint_names(root) == ["ruff", "mypy"]


def test_detects_ruff_from_its_own_config_file():
    root = _tmp()
    (root / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    assert _lint_names(root) == ["ruff"]


def test_detects_mypy_from_setup_cfg_section():
    root = _tmp()
    (root / "setup.cfg").write_text("[mypy]\nstrict = True\n", encoding="utf-8")
    assert _lint_names(root) == ["mypy"]


def test_detects_npm_lint_and_typecheck_scripts():
    root = _tmp()
    (root / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint .", "typecheck": "tsc --noEmit"}}),
        encoding="utf-8",
    )
    assert _lint_names(root) == ["npm run lint", "npm run typecheck"]


def test_detects_tsc_from_tsconfig_with_typescript_installed():
    root = _tmp()
    (root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (root / "node_modules" / "typescript").mkdir(parents=True)
    assert _lint_names(root) == ["tsc"]


def test_detects_cargo_clippy_only_when_configured():
    root = _tmp()
    (root / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    assert _lint_names(root) == []
    (root / "clippy.toml").write_text("msrv = \"1.70\"\n", encoding="utf-8")
    assert _lint_names(root) == ["cargo clippy"]


def test_project_without_linters_detects_exactly_the_checks_it_did_before():
    """Criterion: a project with nothing configured verifies as it does today."""
    root = _tmp()
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "build": "tsc"}}), encoding="utf-8"
    )
    assert [c.name for c in detect_checks(root)] == ["pytest", "npm test", "npm run build"]
    assert _lint_names(root) == []


def test_lint_disabled_detects_no_lint_checks():
    root = _tmp()
    (root / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    assert detect_checks(root, lint=False) == []


def _violation_check(text: str, **kw) -> Check:
    cmd = [sys.executable, "-c", f"import sys; print({text!r}); sys.exit(1)"]
    return Check("ruff", cmd, verify.LINT, suffixes=(".py",), **kw)


def test_lint_failure_fails_the_gate_with_the_tool_output():
    root = _tmp()
    check = _violation_check("app/thing.py:3:1: F821 undefined name 'nope'")
    res = verify.run_lint_check(check, root, ["app/thing.py"], timeout=60)
    assert res.status == FAILED
    assert "F821 undefined name" in res.output
    result = VerificationResult(results=[res])
    assert result.passed is False
    assert "ruff" in result.summary()
    assert "F821" in result.failure_report()


def test_pre_existing_violations_in_untouched_files_do_not_fail_the_run():
    root = _tmp()
    check = _violation_check("legacy/old.py:9:1: E501 line too long")
    res = verify.run_lint_check(check, root, ["app/thing.py"], timeout=60)
    assert res.status == SKIPPED
    assert res.ok is True


def test_lint_check_is_skipped_when_the_run_changed_nothing_it_judges():
    root = _tmp()
    check = _violation_check("whatever")
    res = verify.run_lint_check(check, root, ["README.md"], timeout=60)
    assert res.status == SKIPPED


def test_path_scoped_lint_check_runs_only_on_the_changed_files():
    root = _tmp()
    cmd = [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:])); sys.exit(1)"]
    check = Check("ruff", cmd, verify.LINT, suffixes=(".py",), accepts_paths=True)
    res = verify.run_lint_check(check, root, ["app/thing.py", "docs/readme.md"], timeout=60)
    assert res.status == FAILED
    assert res.output.strip() == "app/thing.py"


def test_verify_runs_detected_lint_checks_against_the_runs_changed_files():
    root = _tmp()
    (root / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    sentinel = Check(
        "ruff",
        [sys.executable, "-c", "import sys; print('app/x.py:1:1: F401'); sys.exit(1)"],
        verify.LINT,
        suffixes=(".py",),
    )
    orig_detect, orig_changed = verify.detect_checks, verify.changed_files
    verify.detect_checks = lambda *a, **k: [sentinel]
    verify.changed_files = lambda *a, **k: ["app/x.py"]
    try:
        result = verify.verify(root, timeout=60)
    finally:
        verify.detect_checks, verify.changed_files = orig_detect, orig_changed
    assert result.passed is False
    assert [r.name for r in result.failures] == ["ruff"]
    assert "F401" in result.failure_report()


def test_the_gate_fails_a_run_whose_change_introduces_lint_errors():
    """Criterion: a lint error in a changed file is recorded as a failed run."""
    from eng_crew.agents.verifier import VerifierAgent
    from eng_crew.config import Settings

    root = _tmp()
    sentinel = Check(
        "ruff",
        [sys.executable, "-c",
         "import sys; print('app/x.py:1:1: F401 unused'); sys.exit(1)"],
        verify.LINT,
        suffixes=(".py",),
    )
    orig_detect, orig_changed = verify.detect_checks, verify.changed_files
    verify.detect_checks = lambda *a, **k: [sentinel]
    verify.changed_files = lambda *a, **k: ["app/x.py"]
    try:
        out = VerifierAgent(Settings(verification_max_fix_attempts=0)).run(
            {"raw_task": "do it", "project_path": str(root), "run_id": 0, "final_summary": ""}
        )
    finally:
        verify.detect_checks, verify.changed_files = orig_detect, orig_changed

    assert out["verification_passed"] is False
    assert "ruff" in out["verification_summary"]
    assert "VERIFICATION FAILED" in out["final_summary"]


# --- what the run introduced, versus what the project already had -------

# A stand-in linter: one finding per line containing BAD, in the format the
# gate asks real tools for (path:line: CODE message).
_FAKE_LINTER = (
    "import sys\n"
    "rows = []\n"
    "for p in sys.argv[1:]:\n"
    "    for i, line in enumerate(open(p, encoding='utf-8'), 1):\n"
    "        if 'BAD' in line:\n"
    "            rows.append(f'{p}:{i}: E999 bad line')\n"
    "print(chr(10).join(rows))\n"
    "sys.exit(1 if rows else 0)\n"
)


def test_new_findings_ignores_a_violation_that_only_moved():
    before = ["app/x.py:4: E999 bad line"]
    after = ["app/x.py:9: E999 bad line"]
    assert verify.new_findings(after, before) == []


def test_new_findings_keeps_a_second_copy_of_an_existing_violation():
    before = ["app/x.py:4: E999 bad line"]
    after = ["app/x.py:4: E999 bad line", "app/x.py:11: E999 bad line"]
    assert verify.new_findings(after, before) == ["app/x.py:11: E999 bad line"]


def _lint_repo() -> Path:
    import subprocess

    root = Path(tempfile.mkdtemp(prefix="engcrew_lint_"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "app").mkdir()
    (root / "app" / "x.py").write_text("x = 1  # BAD\n", encoding="utf-8")
    (root / "app" / "untouched.py").write_text("y = 2  # BAD\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def _fake_lint_check(**kw) -> Check:
    return Check(
        "fakelint",
        [sys.executable, "-c", _FAKE_LINTER],
        verify.LINT,
        suffixes=(".py",),
        accepts_paths=True,
        **kw,
    )


def test_a_violation_that_predates_the_run_does_not_fail_it():
    root = _lint_repo()
    # The run edits the file without adding a violation; the old one just moves.
    (root / "app" / "x.py").write_text("import os\nx = 1  # BAD\n", encoding="utf-8")
    res = verify.run_lint_check(
        _fake_lint_check(), root, verify.changed_files(root), timeout=120, base="HEAD"
    )
    assert res.status == SKIPPED
    assert "predates" in res.output


def test_a_violation_the_run_introduced_fails_it():
    root = _lint_repo()
    (root / "app" / "x.py").write_text("x = 1  # BAD\nz = 3  # BAD\n", encoding="utf-8")
    res = verify.run_lint_check(
        _fake_lint_check(), root, verify.changed_files(root), timeout=120, base="HEAD"
    )
    assert res.status == FAILED
    assert res.output.count("E999") == 1
    assert "app/x.py:2" in res.output.replace("\\", "/")


def test_violations_in_files_the_run_never_touched_are_not_its_problem():
    root = _lint_repo()
    # untouched.py is full of violations, and a whole-project check will find
    # them; only the file this run changed is reported against it.
    (root / "app" / "x.py").write_text("x = 1\n", encoding="utf-8")
    whole_project = Check(
        "fakelint",
        [sys.executable, "-c", _FAKE_LINTER, "app/x.py", "app/untouched.py"],
        verify.LINT,
        suffixes=(".py",),
    )
    res = verify.run_lint_check(
        whole_project, root, verify.changed_files(root), timeout=120, base="HEAD"
    )
    assert res.status == SKIPPED
    assert "did not touch" in res.output


# --- a launch failure is never a skip, wherever the gate launches -------
#
# `_launch` is called from four places: a test/build check, a lint check, the
# lint baseline in the base tree, and the red phase. A positive availability
# probe followed by a launch failure means a broken environment, not a missing
# toolchain, and every one of them must say so rather than skip.


def _boom() -> OSError:
    return OSError(2, "[WinError 2] The system cannot find the file specified")


def test_lint_check_reports_a_launch_failure_instead_of_skipping_it():
    """A launch failure names no changed file, so the "violations in files this
    run did not touch" filter must not quietly turn it into a skip. tsc is the
    case that bites: it is run whole, so the message carries no path at all."""
    boom = _boom()
    real_launch = verify._launch
    real_which = verify.shutil.which

    def explode(cmd, cwd, timeout):
        raise boom

    verify._launch = explode
    verify.shutil.which = lambda exe, *a, **kw: f"/usr/bin/{exe}"
    try:
        check = Check("tsc", ["tsc", "--noEmit"], verify.LINT, suffixes=(".ts",))
        res = verify.run_lint_check(check, _tmp(), ["app/thing.ts"], timeout=30)
    finally:
        verify._launch = real_launch
        verify.shutil.which = real_which

    assert res.status == FAILED
    assert "WinError 2" in res.output


def test_lint_check_still_skips_a_toolchain_that_is_not_installed():
    check = Check("ghostly", ["eng-crew-no-such-tool-zzz", "--version"], verify.LINT,
                  suffixes=(".py",), accepts_paths=True)
    res = verify.run_lint_check(check, _tmp(), ["app/thing.py"], timeout=30)
    assert res.status == SKIPPED
    assert "not installed" in res.output


def test_a_base_tree_launch_failure_leaves_the_run_answering_for_its_findings():
    """The lint baseline runs in a scratch worktree. If the check starts in the
    project but not there, there is no honest baseline: the findings stand, and
    the report says why nothing was compared."""
    root = _lint_repo()
    (root / "app" / "x.py").write_text("x = 1  # BAD\nz = 3  # BAD\n", encoding="utf-8")
    boom = _boom()
    real_launch = verify._launch

    def only_in_the_project(cmd, cwd, timeout):
        if Path(cwd).resolve() != root.resolve():
            raise boom
        return real_launch(cmd, cwd, timeout)

    verify._launch = only_in_the_project
    try:
        res = verify.run_lint_check(
            _fake_lint_check(), root, verify.changed_files(root), timeout=120, base="HEAD"
        )
    finally:
        verify._launch = real_launch

    assert res.status == FAILED
    assert "E999" in res.output, "the run's own findings are still reported"
    assert "could not launch" in res.output
    assert "WinError 2" in res.output


def test_resolved_cmd_docstring_warns_about_cmd_exe_and_fixed_arguments():
    """Resolving to `npm.cmd` hands the command to cmd.exe, which parses the
    arguments. The docstring is where the next reader learns not to pass
    anything a user or a model wrote."""
    doc = (verify.resolved_cmd.__doc__ or "").lower()
    assert "cmd.exe" in doc
    assert "argument" in doc


# --- lint output larger than the stored-report cap ----------------------
#
# run_check caps what it stores so a repair pass is not handed a megabyte of
# linter output. That cap must not reach the baseline comparison: a cut-off
# last line matches nothing in the baseline and gets blamed on the run, and
# findings past the cap are never judged at all.

_CAP = verify.MAX_CHECK_OUTPUT


def _noisy_lint_repo(lines: int = 400) -> Path:
    """A repo whose one changed file already carries more findings than fit."""
    import subprocess

    root = Path(tempfile.mkdtemp(prefix="engcrew_lintcap_"))
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "app").mkdir()
    body = "".join(f"a{i} = {i}  # BAD\n" for i in range(lines))
    (root / "app" / "x.py").write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    assert lines * len("app/x.py:000: E999 bad line\n") > _CAP
    return root


def test_a_run_adding_nothing_to_a_file_full_of_old_findings_passes():
    """Criterion: more pre-existing findings than fit in the cap, none new -> passes."""
    root = _noisy_lint_repo()
    # The run only prepends a line: every old finding shifts down by one, and
    # the full output is far longer than the stored-output cap.
    body = (root / "app" / "x.py").read_text(encoding="utf-8")
    (root / "app" / "x.py").write_text("import os\n" + body, encoding="utf-8")
    res = verify.run_lint_check(
        _fake_lint_check(), root, verify.changed_files(root), timeout=180, base="HEAD"
    )
    assert res.status == SKIPPED, res.output
    assert "predates" in res.output


def test_a_new_finding_past_the_output_cap_still_fails_the_run():
    """Criterion: a new finding beyond the old cut-off fails the run and is named."""
    root = _noisy_lint_repo()
    body = (root / "app" / "x.py").read_text(encoding="utf-8")
    (root / "app" / "x.py").write_text(body + "fresh = 1  # BAD\n", encoding="utf-8")
    changed = verify.changed_files(root)
    res = verify.run_lint_check(_fake_lint_check(), root, changed, timeout=180, base="HEAD")
    assert res.status == FAILED
    # The new violation is on the last line, well past 4000 characters of output.
    assert "app/x.py:401:" in res.output.replace("\\", "/")
    assert "E999" in VerificationResult(results=[res]).failure_report()


def test_the_baseline_comparison_sees_the_uncapped_output_and_only_the_report_is_capped():
    """Criterion: comparison gets both checks whole; the stored report is capped
    and says how many findings it left out."""
    root = _noisy_lint_repo()
    seen: list[int] = []
    orig = verify.run_check

    def spy(check, project_path, timeout=300, **kw):
        res = orig(check, project_path, timeout=timeout, **kw)
        seen.append(len(res.output))
        return res

    # Every finding is new: the base tree has no such file at all.
    (root / "app" / "new.py").write_text(
        "".join(f"b{i} = {i}  # BAD\n" for i in range(400)), encoding="utf-8"
    )
    verify.run_check = spy
    try:
        res = verify.run_lint_check(
            _fake_lint_check(), root, ["app/new.py"], timeout=180, base="HEAD"
        )
    finally:
        verify.run_check = orig
    assert res.status == FAILED
    assert seen and seen[0] > _CAP, seen  # the run's own check was read whole
    assert len(res.output) <= _CAP + 200  # stored result is capped
    assert "omitted" in res.output
    kept = [ln for ln in res.output.splitlines() if "E999" in ln]
    assert 0 < len(kept) < 400
    assert f"{400 - len(kept)} more finding" in res.output


def test_a_capped_report_never_ends_in_a_partial_finding():
    """Criterion: a partial line can never be counted as a finding."""
    findings = [f"app/x.py:{i}: E999 bad line" for i in range(50)]
    text = verify.cap_findings(findings, max_chars=100)
    lines = [ln for ln in text.splitlines() if "E999" in ln]
    assert lines and all(ln in findings for ln in lines)
    assert f"{50 - len(lines)} more findings omitted" in text
    # A single finding longer than the cap is kept whole rather than sliced.
    one = verify.cap_findings(["app/x.py:1: E999 " + "x" * 200], max_chars=20)
    assert "x" * 200 in one


def test_test_and_build_checks_keep_their_existing_output_handling():
    """Criterion: only the lint path changed."""
    root = _tmp()
    noise = [
        sys.executable,
        "-c",
        "import sys; print('E' * 9000); sys.exit(1)",
    ]
    for kind in ("test", "build"):
        res = run_check(Check("noisy", noise, kind), root, timeout=60)
        assert res.status == FAILED
        assert len(res.output) == _CAP
