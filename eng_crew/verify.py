"""Deterministic verification of a project's working tree.

The agent tiers ask the model to run tests; this module *checks*. Nothing here
calls an LLM — it detects what the project can be verified with, runs those
commands, and reports pass/fail. The pipeline uses the result as a gate, so a
run that leaves the tree broken cannot be recorded as a success.

Detection is deliberately conservative: a check is only emitted when the
project clearly supports it, and a missing toolchain yields SKIPPED rather than
FAILED. A project with no detectable checks is "unverified", not "failed". A
toolchain that is *present* but cannot be launched is a different thing, and is
reported FAILED: a skip there would quietly leave the run unverified.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import logging
import re
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

LINT = "lint"  # a Check.kind, alongside "test" and "build"
# Line/column numbers inside a lint finding: noise when comparing against a
# baseline, since an edit anywhere above a violation moves it.
_NUMBERS = re.compile(r"\d+")


@dataclass
class Check:
    """One verification command."""

    name: str
    cmd: list[str]
    kind: str  # "test" | "build" | "lint" | "red"
    # Lint checks only. ``suffixes`` are the file types the check judges, and
    # ``accepts_paths`` says the tool takes the files to check as arguments —
    # both exist so a linter is pointed at what the run changed rather than at
    # a codebase whose pre-existing violations it did not cause.
    suffixes: tuple[str, ...] = ()
    accepts_paths: bool = False


@dataclass
class CheckResult:
    name: str
    kind: str
    status: str
    output: str = ""
    cmd: str = ""
    # The probe found this toolchain and it still would not start. Set so the
    # filters downstream of a check — which look for findings that name the
    # run's own files — cannot mistake the absence of findings for a clean run.
    launch_error: bool = False

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


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


_PY_SUFFIXES = (".py", ".pyi")
_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_ESLINT_CONFIGS = (
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
    ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
    ".eslintrc.yml", ".eslintrc.yaml",
)


def detect_lint_checks(project_path: str | Path) -> list[Check]:
    """Detect the linters and type checkers this project has *configured*.

    Configured is the operative word: a linter that happens to be installed on
    the machine says nothing about whether the project wants it, and running
    one uninvited would fail runs over style the project never adopted. So
    every check here keys off a file in the repo — a config section, a config
    file, an npm script.
    """
    root = Path(project_path).expanduser().resolve()
    checks: list[Check] = []

    # Every command below is asked for one finding per line with the file name
    # on it (--output-format=concise, --format=unix, --message-format=short):
    # the gate attributes findings by reading them, and a tool whose default
    # format puts the path on a separate line cannot be scoped to a run.
    pyproject = _read(root / "pyproject.toml")
    if (
        "[tool.ruff" in pyproject
        or (root / "ruff.toml").exists()
        or (root / ".ruff.toml").exists()
    ):
        checks.append(
            Check(
                name="ruff",
                cmd=[python_for(root), "-m", "ruff", "check", "--output-format=concise"],
                kind=LINT,
                suffixes=_PY_SUFFIXES,
                accepts_paths=True,
            )
        )
    if (
        "[tool.mypy" in pyproject
        or (root / "mypy.ini").exists()
        or (root / ".mypy.ini").exists()
        or "[mypy]" in _read(root / "setup.cfg")
    ):
        checks.append(
            Check(
                name="mypy",
                cmd=[python_for(root), "-m", "mypy"],
                kind=LINT,
                suffixes=_PY_SUFFIXES,
                accepts_paths=True,
            )
        )

    scripts = _npm_scripts(root)
    lint_script = next((k for k in ("lint", "eslint") if scripts.get(k)), None)
    if lint_script:
        checks.append(
            Check(
                name=f"npm run {lint_script}",
                cmd=["npm", "run", lint_script, "--silent"],
                kind=LINT,
                suffixes=_JS_SUFFIXES,
            )
        )
    elif any((root / name).exists() for name in _ESLINT_CONFIGS) and (
        root / "node_modules" / "eslint"
    ).is_dir():
        # --no-install: a gate must never reach the network to fetch a tool.
        checks.append(
            Check(
                name="eslint",
                cmd=["npx", "--no-install", "eslint", "--format=unix"],
                kind=LINT,
                suffixes=_JS_SUFFIXES,
                accepts_paths=True,
            )
        )

    type_script = next(
        (k for k in ("typecheck", "type-check", "tsc") if scripts.get(k)), None
    )
    if type_script:
        checks.append(
            Check(
                name=f"npm run {type_script}",
                cmd=["npm", "run", type_script, "--silent"],
                kind=LINT,
                suffixes=_JS_SUFFIXES,
            )
        )
    elif (root / "tsconfig.json").exists() and (root / "node_modules" / "typescript").is_dir():
        # tsc is given no paths on purpose: passing files overrides tsconfig's
        # own include/compilerOptions, so the project would be checked under
        # different settings than it configured.
        checks.append(
            Check(
                name="tsc",
                cmd=["npx", "--no-install", "tsc", "--noEmit"],
                kind=LINT,
                suffixes=_JS_SUFFIXES,
            )
        )

    if (root / "Cargo.toml").exists() and (
        (root / "clippy.toml").exists()
        or (root / ".clippy.toml").exists()
        or "clippy" in _read(root / "Cargo.toml")
    ):
        checks.append(
            Check(
                name="cargo clippy",
                cmd=["cargo", "clippy", "--quiet", "--all-targets", "--message-format=short"],
                kind=LINT,
                suffixes=(".rs",),
            )
        )

    return checks


def detect_checks(project_path: str | Path, *, lint: bool = True) -> list[Check]:
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

    if lint:
        checks.extend(detect_lint_checks(root))

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


def resolved_cmd(cmd: list[str]) -> list[str]:
    """``cmd`` with its executable resolved to a real path on disk.

    On Windows, npm, eslint and tsc are `.cmd` shims. CreateProcess cannot
    start one from the bare name "npm" — it raises [WinError 2] — so without
    this every JS/TS project's checks died before they ran. ``shutil.which``
    applies PATHEXT and hands back the `npm.cmd` that CreateProcess *can*
    start, which keeps us on ``shell=False``: the arguments still go to the
    process untouched by a shell.

    A command given as a path, or one nothing on PATH answers to, is returned
    as it came.

    One consequence to keep in mind: Windows cannot execute a `.cmd`/`.bat`
    directly either, so CreateProcess runs the resolved shim through cmd.exe,
    which re-parses the argument string with its own metacharacters (`&`,
    `|`, `^`, `"`). ``shell=False`` does not protect the arguments here. Only
    pass arguments eng-crew constructs itself — a detected check's fixed flags
    and repo-relative paths — never text from a user, a model or a project
    file.
    """
    exe = cmd[0]
    seps = [os.sep] + ([os.altsep] if os.altsep else [])
    if any(sep in exe for sep in seps):
        return list(cmd)
    found = shutil.which(exe)
    return [found, *cmd[1:]] if found else list(cmd)


class LaunchFailed(Exception):
    """A check the availability probe found, launched, and would not start."""


def _launch_failure(printable: str, exc: OSError) -> str:
    """How every launch path words a check that would not start.

    The probe said this toolchain is installed, so a launch failure is a real
    problem with the project or the environment, not an absence. Skipping it
    quietly left JS/TS runs on Windows "unverified", and the ticket reviewer
    then refused to judge them.
    """
    return f"could not launch {printable}: {exc}"


def _launch(cmd: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Run a detected check command. Never with a shell."""
    return subprocess.run(
        resolved_cmd(cmd),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )


def run_check(check: Check, project_path: str | Path, timeout: int = 300) -> CheckResult:
    root = Path(project_path).expanduser().resolve()
    is_python = check.cmd[0] == sys.executable or Path(check.cmd[0]).stem.startswith("python")
    printable = ("python " + " ".join(check.cmd[1:])) if is_python else " ".join(check.cmd)

    if not _tool_available(check):
        log.info("verify: %s unavailable — skipping", check.name)
        return CheckResult(check.name, check.kind, SKIPPED, "toolchain not installed", printable)

    try:
        proc = _launch(check.cmd, root, timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            check.name, check.kind, FAILED, f"timed out after {timeout}s", printable
        )
    except OSError as exc:
        return CheckResult(
            check.name,
            check.kind,
            FAILED,
            _launch_failure(printable, exc),
            printable,
            launch_error=True,
        )

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()

    # pytest exit code 5 means "no tests collected" — nothing to verify, not a failure.
    if check.name == "pytest" and proc.returncode == 5:
        return CheckResult(check.name, check.kind, SKIPPED, "no tests collected", printable)

    status = PASSED if proc.returncode == 0 else FAILED
    return CheckResult(check.name, check.kind, status, output[:4000], printable)


def changed_files(project_path: str | Path, base: str = "HEAD") -> list[str]:
    """Repo-relative paths this run changed, for scoping the lint checks.

    Empty when we cannot tell — not in a repo, git missing. A lint check with
    nothing to judge stands down: failing a run over violations it provably did
    not cause is worse than not linting it.
    """
    from . import git_skill

    try:
        return [p.replace("\\", "/") for p in git_skill.changed_paths(project_path, base)]
    except Exception as exc:  # not a repo, git missing, bad base
        log.info("verify: cannot list changed paths (%s)", exc)
        return []


def _blames(line: str, paths: list[str]) -> bool:
    """Does this line of tool output name one of these files?"""
    normalised = line.replace("\\", "/")
    return any(p in normalised for p in paths)


def _fingerprint(line: str) -> str:
    """A finding stripped of what an unrelated edit shifts.

    Line and column numbers move when code above them changes, so comparing
    raw output would call every pre-existing violation new. The file, the rule
    and the message are what identify a finding.
    """
    return _NUMBERS.sub("#", line.replace("\\", "/")).strip()


def new_findings(after: list[str], before: list[str]) -> list[str]:
    """Findings in ``after`` that were not already there in ``before``.

    Compared as a multiset: two copies of a violation where there was one is a
    new one. This is what makes the gate judge what the run introduced rather
    than what the project already lives with.
    """
    counts: dict[str, int] = {}
    for line in before:
        counts[_fingerprint(line)] = counts.get(_fingerprint(line), 0) + 1
    fresh = []
    for line in after:
        key = _fingerprint(line)
        if counts.get(key):
            counts[key] -= 1
            continue
        fresh.append(line)
    return fresh


def _findings_at_base(
    check: Check,
    project_path: str | Path,
    base: str,
    relevant: list[str],
    timeout: int,
) -> list[str] | None:
    """The same check's findings against pre-change code, or None if unknowable.

    A scratch checkout of ``base`` is the cheapest honest baseline: the files
    the run changed, as they were. None means we could not build one — git
    missing, a bad base — and the caller then reports the findings as they are
    rather than silently passing a run.
    """
    from . import git_skill

    root = Path(project_path).expanduser().resolve()
    worktree = None
    try:
        worktree = git_skill.create_detached_worktree(root, base)
        git_skill.link_into_worktree(root, worktree, _LINK_DIRS)
        existing = [p for p in relevant if (worktree / p).is_file()]
        if check.accepts_paths and not existing:
            return []  # every file the check judges is new in this run
        cmd = [*check.cmd, *existing] if check.accepts_paths else list(check.cmd)
        try:
            proc = _launch(cmd, worktree, timeout)
        except OSError as exc:
            # The check started in the project tree, so this is the same broken
            # environment every other launch path reports — not a baseline we
            # merely could not build. Say so rather than compare against silence.
            raise LaunchFailed(_launch_failure(" ".join(cmd), exc)) from exc
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return [ln for ln in output.splitlines() if _blames(ln, existing)]
    except LaunchFailed:
        raise
    except Exception as exc:
        log.info("verify: no lint baseline for %s (%s)", check.name, exc)
        return None
    finally:
        if worktree is not None:
            try:
                git_skill.remove_worktree(root, worktree, force=True)
            except Exception as exc:
                log.info("verify: baseline worktree cleanup failed (%s)", exc)


def run_lint_check(
    check: Check,
    project_path: str | Path,
    changed: list[str],
    timeout: int = 300,
    base: str | None = None,
) -> CheckResult:
    """Run one lint check and hold the run to what it introduced.

    Three filters, in order. The check is pointed at the files the run changed
    (a tool that takes paths is handed them; tsc and clippy need the whole
    project, so they are run whole). Its findings are then narrowed to lines
    naming one of those files — mypy follows imports and will happily report a
    module the run never opened. Whatever survives is compared against the same
    check run against ``base``, so a violation the project already had is not
    this run's to answer for.
    """
    relevant = [p for p in changed if not check.suffixes or p.endswith(check.suffixes)]
    printable = " ".join(check.cmd)
    if not relevant:
        return CheckResult(
            check.name, check.kind, SKIPPED,
            "run changed no files this check judges", printable,
        )

    scoped = check
    if check.accepts_paths:
        scoped = dataclasses.replace(check, cmd=[*check.cmd, *relevant])

    result = run_check(scoped, project_path, timeout=timeout)
    if result.status != FAILED or result.launch_error:
        # A launch failure names no file, so the filters below would read it as
        # "no findings of ours" and skip the check. It stands as a failure.
        return result

    own = [line for line in result.output.splitlines() if _blames(line, relevant)]
    if not own:
        return dataclasses.replace(
            result,
            status=SKIPPED,
            output="only pre-existing violations, in files this run did not touch",
        )

    try:
        before = _findings_at_base(check, project_path, base, relevant, timeout) if base else None
    except LaunchFailed as exc:
        return dataclasses.replace(
            result,
            output=(
                "\n".join(own) + f"\n\nNo baseline to compare against: {exc}"
            )[:4000],
        )
    if before is not None:
        own = new_findings(own, before)
        if not own:
            return dataclasses.replace(
                result,
                status=SKIPPED,
                output="violations, but every one of them predates this run",
            )

    return dataclasses.replace(result, output="\n".join(own)[:4000])


def verify(
    project_path: str | Path,
    *,
    agent_output: str = "",
    truncated: bool = False,
    timeout: int = 300,
    base: str = "HEAD",
    lint: bool = True,
) -> VerificationResult:
    """Run every detected check. ``agent_output`` is scanned for truncation.

    ``truncated`` carries a truncation already established by an earlier pass,
    for a re-verify that deliberately ignores the agent's own narration.
    ``base`` is the commit the run started from; the lint checks are scoped to
    what changed since it.
    """
    truncated = truncated or TRUNCATION_MARKER in (agent_output or "")
    checks = detect_checks(project_path, lint=lint)
    if not checks:
        log.info("verify: no checks detected for %s", project_path)
        return VerificationResult(results=[], truncated=truncated)

    results: list[CheckResult] = []
    changed: list[str] | None = None
    for c in checks:
        if c.kind == LINT:
            if changed is None:
                changed = changed_files(project_path, base)
            results.append(
                run_lint_check(c, project_path, changed, timeout=timeout, base=base)
            )
        else:
            results.append(run_check(c, project_path, timeout=timeout))
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

    # Ignored directories are pruned before they are entered, not filtered
    # after: walking into node_modules is itself what can fail — pnpm's
    # junctions on Windows name paths that do not exist, and rglob raised
    # straight through the gate. An unreadable directory is skipped too.
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _exc: None):
        dirnames[:] = [d for d in dirnames if d not in _TEST_IGNORE_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            try:
                rel_posix = path.relative_to(root).as_posix()
            except ValueError:
                continue
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


def changed_tests(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Test files added, changed or deleted between two snapshots."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


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

    # Probe before building a worktree, so a project without pytest installed
    # is skipped here as it is in every other path — and so a launch failure
    # after this point can only mean a broken environment.
    if not _tool_available(Check(name=name, cmd=cmd, kind=RED)):
        log.info("red: pytest unavailable — skipping")
        return CheckResult(name, RED, SKIPPED, "toolchain not installed", printable)

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
            proc = _launch(cmd, worktree, timeout)
        except subprocess.TimeoutExpired:
            return CheckResult(name, RED, SKIPPED, f"timed out after {timeout}s", printable)
        except OSError as exc:
            return CheckResult(
                name, RED, FAILED, _launch_failure(printable, exc), printable,
                launch_error=True,
            )

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
