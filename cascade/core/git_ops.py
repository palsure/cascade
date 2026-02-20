"""Git operations for Cascade -- branch, commit, diff management."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional


class GitError(Exception):
    pass


class GitOps:
    """Async git operations scoped to a specific repository path."""

    def __init__(self, repo_path: str | Path):
        self.repo = Path(repo_path).resolve()

    async def _run(self, *args: str, check: bool = True) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self.repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {err}")
        return out

    async def init(self) -> str:
        return await self._run("init")

    async def current_branch(self) -> str:
        return await self._run("rev-parse", "--abbrev-ref", "HEAD")

    async def has_repo(self) -> bool:
        try:
            await self._run("rev-parse", "--git-dir")
            return True
        except (GitError, FileNotFoundError):
            return False

    async def create_branch(self, name: str) -> str:
        return await self._run("checkout", "-b", name)

    async def checkout(self, branch: str) -> str:
        return await self._run("checkout", branch)

    async def stage_all(self) -> str:
        return await self._run("add", "-A")

    async def commit(self, message: str) -> str:
        return await self._run("commit", "-m", message)

    async def has_changes(self) -> bool:
        """Check for staged or unstaged changes."""
        result = await self._run("status", "--porcelain", check=False)
        return bool(result.strip())

    async def has_staged_changes(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--cached", "--quiet",
            cwd=str(self.repo),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return proc.returncode != 0

    async def diff(self, staged: bool = False) -> str:
        args = ["diff"]
        if staged:
            args.append("--cached")
        return await self._run(*args)

    async def diff_stat(self, staged: bool = False) -> str:
        args = ["diff", "--stat"]
        if staged:
            args.append("--cached")
        return await self._run(*args)

    async def diff_name_only(self, staged: bool = False) -> list[str]:
        args = ["diff", "--name-only"]
        if staged:
            args.append("--cached")
        result = await self._run(*args)
        return [f for f in result.splitlines() if f.strip()]

    async def log_oneline(self, n: int = 5) -> str:
        return await self._run("log", "--oneline", f"-{n}", check=False)

    async def stash(self) -> str:
        return await self._run("stash")

    async def stash_pop(self) -> str:
        return await self._run("stash", "pop")

    async def push(self, remote: str = "origin", branch: Optional[str] = None) -> str:
        args = ["push", "-u", remote]
        if branch:
            args.append(branch)
        else:
            args.append("HEAD")
        return await self._run(*args)

    async def ensure_clean(self) -> bool:
        """Stash any uncommitted changes and return True if stash was needed."""
        if await self.has_changes():
            await self.stash()
            return True
        return False

    async def ensure_repo(self):
        """Ensure the directory is a git repo, init if not."""
        if not await self.has_repo():
            await self.init()
            await self.stage_all()
            await self.commit("initial commit")
