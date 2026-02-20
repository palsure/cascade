"""
GitHub operations for Cascade -- clone repos, push branches, create PRs.

Uses git CLI for clone/push and gh CLI for PR creation.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class CloneResult:
    repo_url: str
    local_path: str = ""
    name: str = ""
    default_branch: str = "main"
    success: bool = False
    error: str = ""


@dataclass
class PRResult:
    repo_name: str
    branch: str = ""
    pr_url: str = ""
    pr_number: int = 0
    success: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "branch": self.branch,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "success": self.success,
            "error": self.error,
        }


async def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 120) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "", "Command timed out"


def parse_github_url(url: str) -> tuple[str, str]:
    """Extract owner and repo name from various GitHub URL formats.

    Supports:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git
      - owner/repo
    """
    url = url.strip().rstrip("/")

    match = re.match(r"^([\w.-]+)/([\w.-]+)$", url)
    if match:
        return match.group(1), match.group(2).removesuffix(".git")

    match = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
    if match:
        return match.group(1), match.group(2)

    return "", url


def normalize_clone_url(url: str) -> str:
    """Ensure URL is a proper HTTPS clone URL."""
    owner, repo = parse_github_url(url)
    if owner:
        return f"https://github.com/{owner}/{repo}.git"
    return url


async def clone_repo(
    github_url: str,
    workspace_dir: str | Path,
    depth: int = 0,
) -> CloneResult:
    """Clone a GitHub repo into the workspace directory."""
    owner, repo_name = parse_github_url(github_url)
    clone_url = normalize_clone_url(github_url)
    target = Path(workspace_dir) / repo_name

    result = CloneResult(repo_url=github_url, name=repo_name)

    if target.exists() and (target / ".git").is_dir():
        ret, out, err = await _run(["git", "pull", "--rebase"], cwd=str(target))
        result.local_path = str(target)
        result.success = True
        ret2, branch, _ = await _run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(target),
        )
        result.default_branch = branch or "main"
        return result

    target.mkdir(parents=True, exist_ok=True)

    cmd = ["git", "clone"]
    if depth > 0:
        cmd.extend(["--depth", str(depth)])
    cmd.extend([clone_url, str(target)])

    ret, out, err = await _run(cmd, timeout=300)

    if ret != 0:
        result.error = err or "git clone failed"
        return result

    result.local_path = str(target)
    result.success = True

    ret2, branch, _ = await _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(target),
    )
    result.default_branch = branch or "main"

    return result


async def push_branch(repo_dir: str, branch: str) -> tuple[bool, str]:
    """Push a branch to origin."""
    ret, out, err = await _run(
        ["git", "push", "-u", "origin", branch],
        cwd=repo_dir,
        timeout=60,
    )
    if ret != 0:
        return False, err or "push failed"
    return True, out


async def create_pr(
    repo_dir: str,
    title: str,
    body: str,
    base: str = "main",
    head: Optional[str] = None,
) -> PRResult:
    """Create a GitHub PR using the gh CLI."""
    repo_name = Path(repo_dir).name
    result = PRResult(repo_name=repo_name, branch=head or "")

    if not shutil.which("gh"):
        result.error = "gh CLI not installed (install from https://cli.github.com)"
        return result

    cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base,
    ]
    if head:
        cmd.extend(["--head", head])

    ret, out, err = await _run(cmd, cwd=repo_dir, timeout=30)

    if ret != 0:
        result.error = err or "gh pr create failed"
        return result

    result.success = True
    result.pr_url = out.strip()

    pr_match = re.search(r"/pull/(\d+)", result.pr_url)
    if pr_match:
        result.pr_number = int(pr_match.group(1))

    return result


async def detect_language(repo_dir: str) -> str:
    """Heuristic language detection based on files present."""
    p = Path(repo_dir)
    if (p / "package.json").exists():
        return "javascript"
    if (p / "Cargo.toml").exists():
        return "rust"
    if (p / "go.mod").exists():
        return "go"
    if (p / "pom.xml").exists() or (p / "build.gradle").exists():
        return "java"
    for ext in [".py", ".js", ".ts", ".rb", ".rs", ".go", ".java"]:
        if list(p.glob(f"*{ext}")):
            return {
                ".py": "python", ".js": "javascript", ".ts": "typescript",
                ".rb": "ruby", ".rs": "rust", ".go": "go", ".java": "java",
            }.get(ext, "unknown")
    return "unknown"


async def detect_test_cmd(repo_dir: str, language: str) -> str:
    """Heuristic test command detection."""
    p = Path(repo_dir)
    if language == "python":
        if (p / "pytest.ini").exists() or (p / "pyproject.toml").exists() or (p / "setup.py").exists():
            return "python -m pytest -v"
    if language in ("javascript", "typescript"):
        if (p / "package.json").exists():
            return "npm test"
    if language == "rust":
        return "cargo test"
    if language == "go":
        return "go test ./..."
    return ""


async def get_repo_default_branch(repo_dir: str) -> str:
    """Get the default branch name."""
    ret, out, _ = await _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir,
    )
    return out if ret == 0 and out else "main"
