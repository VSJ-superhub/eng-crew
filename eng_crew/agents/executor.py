from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from eng_crew import git_skill, tracker
from eng_crew.agents.base import BaseAgent
from eng_crew.config import Settings
from eng_crew.state import Subtask, TeamState

logger = logging.getLogger(__name__)


class ExecutorAgent(BaseAgent):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    def execute(self, state: TeamState) -> TeamState:
        subtasks: list[Subtask] = list(state["subtasks"])
        run_id: int = state.get("run_id") or 0
        project_path: str = state["project_path"]
        idx: int = state.get("current_subtask_idx", 0)

        subtask: Subtask | None = None
        actual_idx: int = idx
        if 0 <= idx < len(subtasks) and subtasks[idx]["status"] == "reviewed":
            subtask = subtasks[idx]
        else:
            for i, s in enumerate(subtasks):
                if s["status"] == "reviewed" and s.get("review_passed"):
                    subtask = s
                    actual_idx = i
                    break

        if subtask is None:
            logger.warning("ExecutorAgent: no reviewed subtask found — routing to orchestrator")
            return {**state, "_next": "orchestrator"}

        description = subtask.get("description", "")
        patch = subtask.get("patch") or ""

        tracker.update_run_progress(run_id, actual_idx, f"Executing: {description[:80]}")

        patch_success, patch_error = self._apply_patch(project_path, patch)

        if not patch_success:
            logger.error(
                "ExecutorAgent: patch application failed for subtask %s: %s",
                subtask["id"],
                patch_error,
            )
            updated_subtask: Subtask = {
                **subtask,
                "status": "failed",
                "review_feedback": f"Patch application failed: {patch_error}",
            }
            subtasks[actual_idx] = updated_subtask
            failed_count: int = state.get("failed_subtask_count", 0) + 1
            return {
                **state,
                "subtasks": subtasks,
                "current_subtask_idx": actual_idx,
                "failed_subtask_count": failed_count,
                "_next": "orchestrator",
            }

        tests_passed, test_output = self._run_tests(project_path)

        commit_sha = self._commit(project_path, subtask["id"], description)

        execution_results: list[str] = list(state.get("execution_results") or [])
        result_entry = (
            f"[{subtask['id']}] {description[:60]} — "
            f"patch=OK tests={'PASS' if tests_passed else 'FAIL'} sha={commit_sha or 'none'}"
        )
        execution_results.append(result_entry)

        completed_ids: list[str] = list(state.get("completed_subtask_ids") or [])

        if tests_passed:
            logger.info("ExecutorAgent: subtask %s executed successfully (sha=%s)", subtask["id"], commit_sha)
            updated_subtask = {
                **subtask,
                "status": "completed",
                "review_feedback": subtask.get("review_feedback") or "",
            }
            subtasks[actual_idx] = updated_subtask
            completed_ids.append(subtask["id"])
            return {
                **state,
                "subtasks": subtasks,
                "current_subtask_idx": actual_idx,
                "completed_subtask_ids": completed_ids,
                "execution_results": execution_results,
                "_next": "orchestrator",
                "_test_failed": False,
            }

        # Tests failed — mark for retry if budget allows
        test_fix_count: int = state.get("test_fix_count", 0)
        logger.warning(
            "ExecutorAgent: tests failed for subtask %s (fix attempt %d): %s",
            subtask["id"],
            test_fix_count + 1,
            test_output[:200],
        )

        if test_fix_count < 2:
            updated_subtask = {
                **subtask,
                "status": "pending",
                "retry_count": subtask.get("retry_count", 0) + 1,
                "review_feedback": f"Tests failed after patch apply:\n{test_output[:500]}",
            }
            subtasks[actual_idx] = updated_subtask
            return {
                **state,
                "subtasks": subtasks,
                "current_subtask_idx": actual_idx,
                "execution_results": execution_results,
                "test_fix_count": test_fix_count + 1,
                "_next": "dispatcher",
                "_test_failed": True,
            }

        # Exceeded test-fix budget — mark failed
        updated_subtask = {
            **subtask,
            "status": "failed",
            "review_feedback": f"Tests failed after {test_fix_count + 1} fix attempts:\n{test_output[:500]}",
        }
        subtasks[actual_idx] = updated_subtask
        failed_count = state.get("failed_subtask_count", 0) + 1
        return {
            **state,
            "subtasks": subtasks,
            "current_subtask_idx": actual_idx,
            "execution_results": execution_results,
            "failed_subtask_count": failed_count,
            "_next": "orchestrator",
            "_test_failed": True,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_patch(self, project_path: str, patch: str) -> tuple[bool, str]:
        """Apply a unified diff patch via git apply --3way. Returns (success, error_message)."""
        if not patch.strip():
            logger.info("ExecutorAgent._apply_patch: empty patch — treating as no-op success")
            return True, ""

        root = Path(project_path).expanduser().resolve()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".patch", prefix="eng_crew_", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(patch)
            patch_file = fh.name

        try:
            result = subprocess.run(
                ["git", "apply", "--3way", patch_file],
                cwd=root,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return True, ""
            error = (result.stderr.strip() or result.stdout.strip())[:1000]
            return False, error
        except FileNotFoundError:
            return False, "git executable not found"
        finally:
            try:
                Path(patch_file).unlink(missing_ok=True)
            except Exception:
                pass

    def _run_tests(self, project_path: str) -> tuple[bool, str]:
        """Detect and run pytest if available. Returns (passed, output_snippet)."""
        root = Path(project_path).expanduser().resolve()

        # Detect pytest
        has_pytest = False
        for candidate in [root / "pytest.ini", root / "pyproject.toml", root / "setup.cfg"]:
            if candidate.exists():
                has_pytest = True
                break
        if not has_pytest:
            test_dirs = [root / "tests", root / "test"]
            has_pytest = any(d.is_dir() for d in test_dirs)

        if not has_pytest:
            logger.info("ExecutorAgent._run_tests: no pytest detected — skipping tests")
            return True, ""

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--tb=short", "-q", "--no-header"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (result.stdout + result.stderr).strip()
            passed = result.returncode == 0
            return passed, output[:2000]
        except subprocess.TimeoutExpired:
            return False, "pytest timed out after 120s"
        except FileNotFoundError:
            logger.info("ExecutorAgent._run_tests: pytest not installed — skipping")
            return True, ""

    def _commit(self, project_path: str, subtask_id: str, description: str) -> str | None:
        """Stage all changes and commit via git_skill. Returns SHA or None."""
        try:
            message = f"eng-crew: {subtask_id} — {description[:72]}"
            return git_skill.commit_all(project_path, message, add_all=True)
        except git_skill.GitError as exc:
            logger.warning("ExecutorAgent._commit: git commit failed: %s", exc)
            return None
