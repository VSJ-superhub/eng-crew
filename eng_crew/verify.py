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
    timeout: int = 300,
) -> VerificationResult:
    """Run every detected check. ``agent_output`` is scanned for truncation."""
    truncated = TRUNCATION_MARKER in (agent_output or "")
    checks = detect_checks(project_path)
    if not checks:
        log.info("verify: no checks detected for %s", project_path)
        return VerificationResult(results=[], truncated=truncated)

    results = [run_check(c, project_path, timeout=timeout) for c in checks]
    result = VerificationResult(results=results, truncated=truncated)
    log.info("verify: %s", result.summary())
    return result
