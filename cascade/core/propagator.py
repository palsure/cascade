"""
Propagator -- the orchestration engine of Cascade.

For each repository, runs a multi-stage pipeline:
  1. Branch  →  create isolated git branch
  2. Adapt   →  cline -y to implement the change
  3. Test    →  run repo's test command
  4. Fix     →  if tests fail, pipe output to cline -y for repair (retry)
  5. Review  →  git diff | cline --json for self-review
  6. Commit  →  stage and commit changes
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .cline import ClineResult, ClineWrapper
from .config import CascadeConfig, RepoConfig
from .git_ops import GitOps


# ── Status constants ──────────────────────────────────────────────

class Status:
    WAITING = "waiting"
    BRANCHING = "branching"
    ADAPTING = "adapting"
    TESTING = "testing"
    FIXING = "fixing"
    REVIEWING = "reviewing"
    COMMITTING = "committing"
    PUSHING = "pushing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


# ── Per-repo result ──────────────────────────────────────────────

@dataclass
class RepoResult:
    repo_name: str
    repo_path: str
    language: str = ""
    status: str = Status.WAITING
    branch: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    adapt_result: Optional[ClineResult] = None
    test_passed: bool = False
    test_output: str = ""
    review_summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    diff_stat: str = ""
    error: str = ""
    retries_used: int = 0
    pr_url: str = ""
    pushed: bool = False

    @property
    def duration(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 1)

    @property
    def success(self) -> bool:
        return self.status == Status.DONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "language": self.language,
            "status": self.status,
            "branch": self.branch,
            "duration_seconds": self.duration,
            "test_passed": self.test_passed,
            "files_changed": self.files_changed,
            "diff_stat": self.diff_stat,
            "review_summary": self.review_summary[:300],
            "error": self.error,
            "retries_used": self.retries_used,
            "pr_url": self.pr_url,
            "pushed": self.pushed,
        }


# ── Cascade run result ───────────────────────────────────────────

@dataclass
class CascadeResult:
    change_description: str = ""
    repo_results: list[RepoResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 1)

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.repo_results if r.success)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.repo_results if r.status == Status.FAILED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_description": self.change_description[:200],
            "duration_seconds": self.duration,
            "repos_total": len(self.repo_results),
            "repos_success": self.success_count,
            "repos_failed": self.fail_count,
            "repos": [r.to_dict() for r in self.repo_results],
        }


# ── Event callback type ──────────────────────────────────────────

EventCallback = Callable[[str, dict[str, Any]], Any]


# ── Propagator ───────────────────────────────────────────────────

class Propagator:
    """
    Orchestrates Cline CLI agents across multiple repositories.

    Pipeline per repo:
      branch → adapt (cline -y) → test → [fix loop] → review (git diff | cline --json) → commit
    """

    def __init__(
        self,
        config: CascadeConfig,
        cline: Optional[ClineWrapper] = None,
        on_event: Optional[EventCallback] = None,
        adapt_prompt_template: Optional[str] = None,
        verify_prompt_template: Optional[str] = None,
        fix_prompt_template: Optional[str] = None,
    ):
        self.config = config
        self.cline = cline or ClineWrapper(
            max_concurrent=config.settings.max_parallel,
            default_timeout=config.settings.timeout_per_repo,
        )
        self.on_event = on_event
        self.adapt_template = adapt_prompt_template
        self.verify_template = verify_prompt_template
        self.fix_template = fix_prompt_template

    async def _emit(self, event_type: str, data: dict):
        if self.on_event:
            try:
                result = self.on_event(event_type, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    # ── Main entry ───────────────────────────────────────────

    async def run(
        self,
        change_description: str,
        dry_run: bool = False,
    ) -> CascadeResult:
        cascade_result = CascadeResult(
            change_description=change_description,
            started_at=time.time(),
        )

        await self._emit("cascade.started", {
            "change": change_description[:200],
            "repos": [r.name for r in self.config.repos],
        })

        sem = asyncio.Semaphore(self.config.settings.max_parallel)
        tasks = []

        for repo_cfg in self.config.repos:
            repo_result = RepoResult(
                repo_name=repo_cfg.name,
                repo_path=str(repo_cfg.resolved_path),
                language=repo_cfg.language,
            )
            cascade_result.repo_results.append(repo_result)

            async def _handle(rc=repo_cfg, rr=repo_result):
                async with sem:
                    await self._handle_repo(rc, rr, change_description, dry_run)

            tasks.append(asyncio.create_task(_handle()))

        await asyncio.gather(*tasks, return_exceptions=True)

        cascade_result.finished_at = time.time()
        await self._emit("cascade.completed", cascade_result.to_dict())
        return cascade_result

    # ── Per-repo pipeline ────────────────────────────────────

    async def _handle_repo(
        self,
        repo_cfg: RepoConfig,
        result: RepoResult,
        change_description: str,
        dry_run: bool,
    ):
        result.started_at = time.time()
        git = GitOps(repo_cfg.resolved_path)
        settings = self.config.settings

        try:
            # 0. Ensure git repo exists
            await git.ensure_repo()
            base_branch = await git.current_branch()

            # 1. Create branch
            result.status = Status.BRANCHING
            branch_name = f"{settings.branch_prefix}{repo_cfg.name}"
            try:
                await git.create_branch(branch_name)
            except Exception:
                await git.checkout(branch_name)
            result.branch = branch_name
            await self._emit("repo.branching", result.to_dict())

            if dry_run:
                result.status = Status.SKIPPED
                result.finished_at = time.time()
                await self._emit("repo.skipped", result.to_dict())
                await git.checkout(base_branch)
                return

            # 2. Adapt -- cline -y -c <repo> "apply change"
            result.status = Status.ADAPTING
            await self._emit("repo.adapting", result.to_dict())

            adapt_prompt = self._build_adapt_prompt(change_description, repo_cfg)

            adapt_result = await self.cline.invoke(
                prompt=adapt_prompt,
                cwd=str(repo_cfg.resolved_path),
                yolo=True,
                model=settings.model or None,
                timeout=settings.timeout_per_repo,
                on_output=lambda line: self._emit("repo.output", {
                    "repo": repo_cfg.name, "line": line,
                }),
            )
            result.adapt_result = adapt_result

            if not adapt_result.success:
                result.status = Status.FAILED
                result.error = adapt_result.error or "Cline adaptation failed"
                result.finished_at = time.time()
                await self._emit("repo.failed", result.to_dict())
                await git.checkout(base_branch)
                return

            # Check if Cline made any changes
            if not await git.has_changes():
                result.status = Status.SKIPPED
                result.error = "No file changes produced"
                result.finished_at = time.time()
                await self._emit("repo.skipped", result.to_dict())
                await git.checkout(base_branch)
                return

            # 3. Test
            if repo_cfg.test_cmd:
                result.status = Status.TESTING
                await self._emit("repo.testing", result.to_dict())

                test_passed, test_output = await self._run_tests(repo_cfg)
                result.test_output = test_output

                # 4. Fix loop on failure
                retries = 0
                while not test_passed and settings.retry_on_test_fail and retries < settings.max_retries:
                    retries += 1
                    result.status = Status.FIXING
                    result.retries_used = retries
                    await self._emit("repo.fixing", {**result.to_dict(), "retry": retries})

                    fix_prompt = self._build_fix_prompt(
                        change_description, repo_cfg, test_output,
                    )
                    await self.cline.invoke(
                        prompt=fix_prompt,
                        cwd=str(repo_cfg.resolved_path),
                        yolo=True,
                        model=settings.model or None,
                        timeout=settings.timeout_per_repo,
                    )

                    test_passed, test_output = await self._run_tests(repo_cfg)
                    result.test_output = test_output

                result.test_passed = test_passed
                if not test_passed:
                    result.status = Status.FAILED
                    result.error = "Tests failed after retries"
                    result.finished_at = time.time()
                    await self._emit("repo.failed", result.to_dict())
                    await git.checkout(base_branch)
                    return
            else:
                result.test_passed = True

            # 5. Self-review -- git diff | cline --json "review"
            result.status = Status.REVIEWING
            await self._emit("repo.reviewing", result.to_dict())

            diff_output = await git.diff()
            if diff_output:
                review_prompt = self._build_verify_prompt(change_description)
                review_result = await self.cline.invoke(
                    prompt=review_prompt,
                    json_output=True,
                    stdin_data=diff_output,
                    model=settings.model or None,
                    timeout=120,
                )
                result.review_summary = review_result.text_output

            # 6. Stage and commit
            result.status = Status.COMMITTING
            await git.stage_all()

            if await git.has_staged_changes():
                result.files_changed = await git.diff_name_only(staged=True)
                result.diff_stat = await git.diff_stat(staged=True)
                commit_msg = f"cascade: {change_description[:60]}"
                await git.commit(commit_msg)

            # 7. Push branch + create PR (for GitHub repos)
            if repo_cfg.is_github:
                result.status = Status.PUSHING
                await self._emit("repo.pushing", result.to_dict())
                try:
                    from .github_ops import push_branch, create_pr

                    ok, push_err = await push_branch(
                        str(repo_cfg.resolved_path), branch_name,
                    )
                    if ok:
                        result.pushed = True
                        pr_body = (
                            f"## Cascade Auto-Propagation\n\n"
                            f"**Change:** {change_description}\n\n"
                            f"**Files changed:** {len(result.files_changed)}\n\n"
                            f"---\n*Created by [Cascade](https://github.com) "
                            f"using Cline CLI as infrastructure.*"
                        )
                        pr_result = await create_pr(
                            str(repo_cfg.resolved_path),
                            title=f"cascade: {change_description[:60]}",
                            body=pr_body,
                            base=base_branch,
                            head=branch_name,
                        )
                        if pr_result.success:
                            result.pr_url = pr_result.pr_url
                except Exception as exc:
                    await self._emit("repo.output", {
                        "repo": repo_cfg.name,
                        "line": f"Push/PR warning: {exc}\n",
                    })

            # Done
            result.status = Status.DONE
            result.finished_at = time.time()
            await self._emit("repo.done", result.to_dict())

            # Return to base branch
            await git.checkout(base_branch)

        except Exception as exc:
            result.status = Status.FAILED
            result.error = str(exc)
            result.finished_at = time.time()
            await self._emit("repo.failed", result.to_dict())
            try:
                await git.checkout(base_branch)
            except Exception:
                pass

    # ── Test runner ──────────────────────────────────────────

    async def _run_tests(self, repo_cfg: RepoConfig) -> tuple[bool, str]:
        if not repo_cfg.test_cmd:
            return True, ""

        proc = await asyncio.create_subprocess_shell(
            repo_cfg.test_cmd,
            cwd=str(repo_cfg.resolved_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            output = stdout.decode("utf-8", errors="replace")
            return proc.returncode == 0, output
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "Test command timed out"

    # ── Prompt builders ──────────────────────────────────────

    def _build_adapt_prompt(self, change: str, repo: RepoConfig) -> str:
        if self.adapt_template:
            return (
                self.adapt_template
                .replace("{{CHANGE}}", change)
                .replace("{{REPO_NAME}}", repo.name)
                .replace("{{LANGUAGE}}", repo.language)
            )
        return (
            f"Apply the following API/schema change to this {repo.language} codebase:\n\n"
            f"CHANGE: {change}\n\n"
            f"Instructions:\n"
            f"1. Find all references to the old fields/API shape.\n"
            f"2. Update models, types, and data classes.\n"
            f"3. Update all code that reads or writes the affected fields.\n"
            f"4. Update tests to match the new shape.\n"
            f"5. Update any documentation or comments.\n"
            f"6. Make minimal, surgical changes -- do not refactor unrelated code.\n"
            f"7. After making changes, verify the code is syntactically valid."
        )

    def _build_fix_prompt(self, change: str, repo: RepoConfig, test_output: str) -> str:
        if self.fix_template:
            return (
                self.fix_template
                .replace("{{CHANGE}}", change)
                .replace("{{REPO_NAME}}", repo.name)
                .replace("{{TEST_OUTPUT}}", test_output[:3000])
            )
        return (
            f"The tests are failing after applying this change:\n\n"
            f"CHANGE: {change}\n\n"
            f"TEST OUTPUT:\n{test_output[:3000]}\n\n"
            f"Fix the failing tests. Only change what is necessary to make tests pass."
        )

    def _build_verify_prompt(self, change: str) -> str:
        if self.verify_template:
            return self.verify_template.replace("{{CHANGE}}", change)
        return (
            f"Review the following code diff for correctness.\n"
            f"The intended change was: {change}\n\n"
            f"Check for:\n"
            f"- Missing updates (fields still referencing the old name)\n"
            f"- Broken logic or type mismatches\n"
            f"- Test coverage of the change\n\n"
            f"Provide a brief summary of your review."
        )
