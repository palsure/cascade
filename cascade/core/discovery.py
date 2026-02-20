"""
Discovery phase -- uses Cline CLI in --json mode to identify
files affected by a change in each repository.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .cline import ClineResult, ClineWrapper
from .config import RepoConfig


DISCOVER_PROMPT_TEMPLATE = """\
Analyze this repository and list ALL files that would need to change \
for the following API/schema change:

CHANGE: {change_description}

For each affected file, explain briefly what needs to change. \
Be thorough -- check imports, type definitions, function calls, \
tests, documentation, and configuration files.

Output your analysis as a structured list."""


@dataclass
class DiscoveryResult:
    repo_name: str
    repo_path: str
    cline_result: Optional[ClineResult] = None
    affected_files: list[str] = field(default_factory=list)
    analysis: str = ""
    error: str = ""
    duration_seconds: float = 0.0

    @property
    def success(self) -> bool:
        return not self.error and self.cline_result is not None and self.cline_result.success

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "success": self.success,
            "affected_files": self.affected_files,
            "analysis_preview": self.analysis[:500],
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 2),
        }


async def discover_repo(
    repo: RepoConfig,
    change_description: str,
    cline: ClineWrapper,
    prompt_template: Optional[str] = None,
    model: Optional[str] = None,
    on_event: Optional[Callable] = None,
) -> DiscoveryResult:
    """
    Run discovery on a single repo using cline --json.

    Uses: cline --json -c <repo_path> "discover prompt"
    """
    result = DiscoveryResult(repo_name=repo.name, repo_path=str(repo.resolved_path))

    template = prompt_template or DISCOVER_PROMPT_TEMPLATE
    prompt = template.format(change_description=change_description)

    if on_event:
        await _emit(on_event, "discovery.started", {
            "repo": repo.name,
            "path": str(repo.resolved_path),
        })

    try:
        cline_result = await cline.invoke(
            prompt=prompt,
            cwd=str(repo.resolved_path),
            json_output=True,
            model=model,
            timeout=120,
        )
        result.cline_result = cline_result
        result.duration_seconds = cline_result.duration_seconds

        if cline_result.success:
            result.analysis = cline_result.text_output
            result.affected_files = _extract_file_paths(
                cline_result.text_output, repo.resolved_path
            )
        else:
            result.error = cline_result.error or "Cline invocation failed"

    except Exception as exc:
        result.error = str(exc)

    if on_event:
        await _emit(on_event, "discovery.completed", result.to_dict())

    return result


async def discover_all(
    repos: list[RepoConfig],
    change_description: str,
    cline: ClineWrapper,
    prompt_template: Optional[str] = None,
    model: Optional[str] = None,
    max_parallel: int = 4,
    on_event: Optional[Callable] = None,
) -> list[DiscoveryResult]:
    """Run discovery across all repos in parallel."""
    sem = asyncio.Semaphore(max_parallel)

    async def _discover(repo: RepoConfig) -> DiscoveryResult:
        async with sem:
            return await discover_repo(
                repo, change_description, cline, prompt_template, model, on_event,
            )

    tasks = [asyncio.create_task(_discover(r)) for r in repos]
    return await asyncio.gather(*tasks)


def _extract_file_paths(text: str, repo_root: Path) -> list[str]:
    """Heuristic extraction of file paths mentioned in Cline's analysis."""
    import re

    candidates: list[str] = []
    patterns = [
        r'`([^`]+\.[a-zA-Z]{1,10})`',
        r'[\s:]+([a-zA-Z_./][\w./\-]*\.[a-zA-Z]{1,10})',
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, text):
            path_str = match.group(1).strip()
            if "/" in path_str or "." in path_str:
                full = repo_root / path_str
                if full.exists():
                    candidates.append(path_str)

    return list(dict.fromkeys(candidates))


async def _emit(callback: Callable, event_type: str, data: dict):
    try:
        result = callback(event_type, data)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        pass
