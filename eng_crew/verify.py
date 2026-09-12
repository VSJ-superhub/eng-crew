"""Deterministic verification of a project's working tree.

The agent tiers ask the model to run tests; this module *checks*. Nothing here
calls an LLM — it detects what the project can be verified with, runs those
commands, and reports pass/fail. The pipeline uses the result as a gate, so a
run that leaves the tree broken cannot be recorded as a success.

Detection is deliberately conservative: a check is only emitted when the
project clearly supports it, and a missing toolchain yields SKIPPED rather than
FAILED. A project with no detectable checks is "unverified", not "failed".
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Marker written by providers.claude_cli when a CLI call exhausts its turn
# budget. A truncated implementation must never pass the gate.
TRUNCATION_MARKER = "[TRUNCATED:"

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class Check:
    """One verification command."""

    name: str
    cmd: list[str]
    kind: str  # "test" | "build"


@dataclass
class CheckResult:
    name: str
    kind: str
    status: str
    output: str = ""
    cmd: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (PASSED, SKIPPED)


@dataclass
class VerificationResult:
    results: list[CheckResult] = field(default_factory=list)
    truncated: bool = False

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == FAILED]

    @property
    def ran_any(self) -> bool:
        return any(r.status in (PASSED, FAILED) for r in self.results)

    @property
    def passed(self) -> bool:
        """True when nothing failed. Truncated output is always a failure."""
        return not self.truncated and not self.failures

    @property
    def unverified(self) -> bool:
        """Passed only because there was nothing to run."""
        return self.passed and not self.ran_any

    def summary(self) -> str:
        if self.truncated and not self.failures:
            return "FAILED — agent output was truncated before completion"
        if self.failures:
            names = ", ".join(r.name for r in self.failures)
            prefix = "FAILED (truncated output; " if self.truncated else "FAILED ("
            return f"{prefix}{names})"
        if self.unverified:
            return "UNVERIFIED — no test or build command detected"
        ran = [r.name for r in self.results if r.status == PASSED]
        return f"PASSED ({', '.join(ran)})"

    def failure_report(self, max_chars: int = 4000) -> str:
        """Failure output, for feeding back to a repair pass."""
        chunks = []
        if self.truncated:
            chunks.append(
                "The previous attempt hit its turn limit and stopped mid-implementation. "
                "Finish the incomplete work."
            )
        for r in self.failures:
            chunks.append(f"--- {r.name} (`{r.cmd}`) ---\n{r.output}")
        report = "\n\n".join(chunks)
        return report[:max_chars]


def _has_python_tests(root: Path) -> bool:
    if (root / "pytest.ini").exists() or (root / "setup.cfg").exists():
        return True
    if (root / "tests").is_dir() or (root / "test").is_dir():
        return True
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            return "pytest" in pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
    return False


def _npm_scripts(root: Path) -> dict:
    pkg = root / "package.json"
    if not pkg.exists():
        return {}
    try:
        data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    scripts = data.get("scripts")
    return scripts if isinstance(scripts, dict) else {}


# `npm init` writes this placeholder; running it is not a real test.
_NPM_PLACEHOLDER = "no test specified"


def python_for(root: Path) -> str:
    """The interpreter to verify a Python project with.

    A project's test dependencies live in its own virtualenv, not in whatever
    interpreter happens to be running eng-crew — preferring the project venv is
    the difference between running the tests and skipping them.
    """
    for rel in ((".venv", "Scripts", "python.exe"), (".venv", "bin", "python"),
                ("venv", "Scripts", "python.exe"), ("venv", "bin", "python")):
        candidate = root.joinpath(*rel)
        if candidate.exists():
            return str(candidate)
    return sys.executable


def detect_checks(project_path: str | Path) -> list[Check]:
    """Detect the verification commands this project supports."""
    root = Path(project_path).expanduser().resolve()
    checks: list[Check] = []

    if _has_python_tests(root):
        checks.append(
            Check(
                name="pytest",
                cmd=[python_for(root), "-m", "pytest", "-q", "--no-header", "--tb=short"],
                kind="test",
            )
        )

    scripts = _npm_scripts(root)
    test_script = scripts.get("test", "")
    if test_script and _NPM_PLACEHOLDER not in test_script:
        checks.append(Check(name="npm test", cmd=["npm", "test", "--silent"], kind="test"))
    if scripts.get("build"):
        checks.append(Check(name="npm run build", cmd=["npm", "run", "build"], kind="build"))

    if (root / "Cargo.toml").exists():
        checks.append(Check(name="cargo test", cmd=["cargo", "test", "--quiet"], kind="test"))

    if (root / "go.mod").exists():
        checks.append(Check(name="go test", cmd=["go", "test", "./..."], kind="test"))

    return checks


def _tool_available(check: Check) -> bool:
    """Is the executable for this check present?"""
    exe = check.cmd[0]
    if exe == sys.executable or Path(exe).stem.startswith("python"):
        # `python -m <module>`: the interpreter obviously exists, so probe the
        # module instead. Any other python invocation needs no probe.
        if "-m" in check.cmd:
            module = check.cmd[check.cmd.index("-m") + 1]
            probe = subprocess.run(
                [exe, "-c", f"import {module}"],
                capture_output=True,
                text=True,
            )
            return probe.returncode == 0
        return True
    return shutil.which(exe) is not None


def run_check(check: Check, project_path: str | Path, timeout: int = 300) -> CheckResult:
    root = Path(project_path).expanduser().resolve()
    is_python = check.cmd[0] == sys.executable or Path(check.cmd[0]).stem.startswith("python")
    printable = ("python " + " ".join(check.cmd[1:])) if is_python else " ".join(check.cmd)

    if not _tool_available(check):
        log.info("verify: %s unavailable — skipping", check.name)
        return CheckResult(check.name, check.kind, SKIPPED, "toolchain not installed", printable)

    try:
        proc = subprocess.run(
            check.cmd,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            check.name, check.kind, FAILED, f"timed out after {timeout}s", printable
        )
    except OSError as exc:
        # Executable vanished between the which() probe and the call.
        return CheckResult(check.name, check.kind, SKIPPED, str(exc), printable)

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()

    # pytest exit code 5 means "no tests collected" — nothing to verify, not a failure.
    if check.name == "pytest" and proc.returncode == 5:
        return CheckResult(check.name, check.kind, SKIPPED, "no tests collected", printable)

    status = PASSED if proc.returncode == 0 else FAILED
    return CheckResult(check.name, check.kind, status, output[:4000], printable)


def verify(
    project_path: str | Path,
    *,
    agent_output: str = "",
    truncated: bool = False,
    timeout: int = 300,
) -> VerificationResult:
    """Run every detected check. ``agent_output`` is scanned for truncation.

    ``truncated`` carries a truncation already established by an earlier pass,
    for a re-verify that deliberately ignores the agent's own narration.
    """
    truncated = truncated or TRUNCATION_MARKER in (agent_output or "")
    checks = detect_checks(project_path)
    if not checks:
        log.info("verify: no checks detected for %s", project_path)
        return VerificationResult(results=[], truncated=truncated)

    results = [run_check(c, project_path, timeout=timeout) for c in checks]
    result = VerificationResult(results=results, truncated=truncated)
    log.info("verify: %s", result.summary())
    return result


# ----------------------------------------------------------------------
# Test lock
#
# A repair pass is handed failing tests and told to fix them. The cheap way
# out is to edit the test instead of the code, which turns the gate green
# while destroying the evidence it was built to collect. The lock snapshots
# the test files before repair and refuses a run that quietly rewrote one.
#
# Files implicated in the failure output are exempt: when the agent's own
# new test is what's broken, editing it is the correct fix. Everything the
# failure never mentioned is frozen.
# ----------------------------------------------------------------------

_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})
_TEST_IGNORE_DIRS = frozenset(
    {
        ".git", "__pycache__", "node_modules", ".venv", "venv",
        "dist", "build", ".eng-crew", ".mypy_cache", ".ruff_cache",
        ".pytest_cache", "target", "vendor",
    }
)
# Suffixes that mark a test file wherever it sits in the tree.
_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.rs", "_test.js", "_test.ts",
    ".test.js", ".test.jsx", ".test.ts", ".test.tsx",
    ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
)

LOCK_STRICT = "strict"
LOCK_WARN = "warn"
LOCK_OFF = "off"


def is_test_path(rel_path: str) -> bool:
    """Does this repo-relative path look like a test file?"""
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel:
        return False
    parts = rel.split("/")
    name = parts[-1]
    if any(part in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith(_TEST_SUFFIXES)


def snapshot_tests(project_path: str | Path) -> dict[str, str]:
    """Map every test file in the tree to a hash of its contents.

    Unreadable files are skipped rather than raising: the lock is a safety
    net, and it must not be able to crash the gate it protects.
    """
    root = Path(project_path).expanduser().resolve()
    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot

    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(root)
        except (OSError, ValueError):
            continue
        if any(part in _TEST_IGNORE_DIRS for part in rel.parts):
            continue
        rel_posix = rel.as_posix()
        if not is_test_path(rel_posix):
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        snapshot[rel_posix] = digest
    return snapshot


def _mentioned(rel_posix: str, text: str) -> bool:
    """Is this file named in the failure output?

    pytest node IDs always use forward slashes; other runners emit native
    separators. Check both — a missed exemption would fail an honest run.
    """
    if not text:
        return False
    if rel_posix in text:
        return True
    return rel_posix.replace("/", "\\") in text


def lock_violations(
    before: dict[str, str],
    after: dict[str, str],
    exempt_output: str = "",
) -> list[str]:
    """Test files changed or deleted since ``before`` that the failures never named."""
    violations = []
    for rel_posix, digest in sorted(before.items()):
        if after.get(rel_posix) == digest:
            continue
        if _mentioned(rel_posix, exempt_output):
            continue
        violations.append(rel_posix)
    return violations


# ----------------------------------------------------------------------
# Red phase
#
# The gate proves a run did not break what was already covered. It cannot
# prove the run did what was asked: a change plus a test that would have
# passed anyway is indistinguishable from real work.
#
# So replay the run's new tests against the code as it stood before the run.
# They have to fail there. A test that passes without the change is not
# testing the change — it is decoration, and the gate should say so.
#
# The execution tiers leave their output uncommitted (pipeline commits only
# after verification), so "before the run" is just HEAD, and a detached
# worktree gives it to us for the price of a checkout.
# ----------------------------------------------------------------------

RED = "red"  # a third Check.kind, alongside "test" and "build"

RED_STRICT = "strict"
RED_WARN = "warn"
RED_OFF = "off"

# Linked into the scratch worktree so the replayed tests can actually import
# their dependencies; a fresh checkout has neither.
_LINK_DIRS = [".venv", "venv", "node_modules"]


def _is_support_file(rel_posix: str) -> bool:
    """conftest.py carries the fixtures a test needs; replay it alongside."""
    return rel_posix.rsplit("/", 1)[-1] == "conftest.py"


def new_test_files(project_path: str | Path, base: str = "HEAD") -> list[str]:
    """Test files this run added or modified, plus any conftest it touched."""
    from . import git_skill

    try:
        changed = git_skill.changed_paths(project_path, base)
    except Exception as exc:  # not a repo, git missing, detached weirdness
        log.info("red: cannot list changed paths (%s)", exc)
        return []

    return [
        p.replace("\\", "/")
        for p in changed
        if is_test_path(p) or _is_support_file(p.replace("\\", "/"))
    ]


def _copy_into(worktree: Path, project_root: Path, rel_paths: list[str]) -> list[str]:
    """Copy the run's versions of these files over the pre-change checkout."""
    copied = []
    for rel in rel_paths:
        source = project_root / rel
        dest = worktree / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
            copied.append(rel)
        except OSError as exc:
            log.info("red: could not stage %s (%s)", rel, exc)
    return copied


def verify_red(
    project_path: str | Path,
    *,
    base: str = "HEAD",
    timeout: int = 300,
) -> CheckResult:
    """Replay this run's new tests against pre-change code.

    PASSED means they failed there, which is what we want: the tests
    discriminate. FAILED means they passed without the change.
    """
    from . import git_skill

    root = Path(project_path).expanduser().resolve()
    name = "red phase"

    if not any(c.name == "pytest" for c in detect_checks(root)):
        return CheckResult(name, RED, SKIPPED, "red phase supports pytest projects only", "")

    tests = new_test_files(root, base)
    only_support = tests and all(_is_support_file(t) for t in tests)
    if not tests or only_support:
        return CheckResult(name, RED, SKIPPED, "run added no new tests", "")

    replayed = [t for t in tests if not _is_support_file(t)]
    cmd = [python_for(root), "-m", "pytest", "-q", "--no-header", "--tb=line", *replayed]
    printable = "python -m pytest " + " ".join(replayed) + f"  (against {base})"

    worktree = None
    try:
        worktree = git_skill.create_detached_worktree(root, base)
    except Exception as exc:
        # A scratch checkout is a convenience, not a guarantee. Never fail a
        # run because we could not build one.
        log.info("red: worktree unavailable (%s)", exc)
        return CheckResult(name, RED, SKIPPED, f"could not create base worktree: {exc}", printable)

    try:
        git_skill.link_into_worktree(root, worktree, _LINK_DIRS)
        staged = _copy_into(worktree, root, tests)
        if not any(not _is_support_file(s) for s in staged):
            return CheckResult(name, RED, SKIPPED, "could not stage tests into base tree", printable)

        try:
            proc = subprocess.run(
                cmd,
                cwd=worktree,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return CheckResult(name, RED, SKIPPED, f"timed out after {timeout}s", printable)
        except OSError as exc:
            return CheckResult(name, RED, SKIPPED, str(exc), printable)

        output = ((proc.stdout or "") + (proc.stderr or "")).strip()

        if proc.returncode == 0:
            return CheckResult(
                name,
                RED,
                FAILED,
                "These tests pass against the code as it was before this run, so "
                "they do not demonstrate the change works:\n  "
                + "\n  ".join(replayed)
                + "\n\nWrite a test that fails without the change.\n\n"
                + output[:2000],
                printable,
            )

        # Exit 5 is "no tests collected" — at base that usually means the test
        # file imports something this run created, which is a legitimate red.
        detail = "collected nothing at base" if proc.returncode == 5 else "failed at base"
        return CheckResult(name, RED, PASSED, f"{len(replayed)} new test file(s) {detail}", printable)
    finally:
        if worktree is not None:
            try:
                git_skill.remove_worktree(root, worktree, force=True)
            except Exception as exc:
                log.info("red: worktree cleanup failed for %s (%s)", worktree, exc)
