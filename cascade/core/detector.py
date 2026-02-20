"""
Change detection -- scans repos for schema drift between source and consumers.

Compares field patterns across repos to detect when the source (backend) has
been updated but consumer repos still reference the old schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CascadeConfig, RepoConfig

OLD_FIELDS = ["first_name", "last_name", "author_first_name", "author_last_name"]
NEW_FIELDS = ["full_name", "author_name"]

CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css",
    ".json", ".yaml", ".yml", ".md", ".txt",
}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache", "venv"}


@dataclass
class FieldRef:
    file: str
    line_num: int
    line_text: str
    field_name: str


@dataclass
class RepoAnalysis:
    repo_name: str
    role: str
    language: str
    old_refs: list[FieldRef] = field(default_factory=list)
    new_refs: list[FieldRef] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def has_old(self) -> bool:
        return len(self.old_refs) > 0

    @property
    def has_new(self) -> bool:
        return len(self.new_refs) > 0

    @property
    def affected_files(self) -> list[str]:
        return sorted({r.file for r in self.old_refs})

    def get_display_status(self, source_updated: bool) -> str:
        """Context-aware status that depends on whether source has migrated."""
        if self.role == "source":
            if self.has_new and not self.has_old:
                return "updated"
            return "original"
        if source_updated:
            if self.has_old:
                return "out_of_sync"
            if self.has_new:
                return "synced"
            return "clean"
        else:
            if self.has_old:
                return "current"
            return "clean"

    def to_dict(self, source_updated: bool = False) -> dict[str, Any]:
        return {
            "name": self.repo_name,
            "role": self.role,
            "language": self.language,
            "status": self.get_display_status(source_updated),
            "files_scanned": self.files_scanned,
            "old_field_count": len(self.old_refs),
            "new_field_count": len(self.new_refs),
            "affected_files": self.affected_files,
            "old_refs": [
                {"file": r.file, "line": r.line_num, "field": r.field_name, "text": r.line_text}
                for r in self.old_refs[:30]
            ],
            "new_refs": [
                {"file": r.file, "line": r.line_num, "field": r.field_name, "text": r.line_text}
                for r in self.new_refs[:30]
            ],
        }


@dataclass
class DriftReport:
    status: str  # "in_sync" | "drift_detected"
    analyses: list[RepoAnalysis] = field(default_factory=list)
    change_summary: str = ""
    affected_count: int = 0
    total_old_refs: int = 0

    def to_dict(self) -> dict[str, Any]:
        source_updated = self.status == "drift_detected"
        return {
            "status": self.status,
            "change_summary": self.change_summary,
            "affected_count": self.affected_count,
            "total_old_refs": self.total_old_refs,
            "repos": [a.to_dict(source_updated=source_updated) for a in self.analyses],
        }


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def scan_repo(repo: RepoConfig) -> RepoAnalysis:
    """Scan a single repo for old and new field pattern references."""
    analysis = RepoAnalysis(
        repo_name=repo.name, role=repo.role, language=repo.language,
    )
    root = repo.resolved_path
    if not root.is_dir():
        return analysis

    old_patterns = {f: re.compile(r"\b" + re.escape(f) + r"\b") for f in OLD_FIELDS}
    new_patterns = {f: re.compile(r"\b" + re.escape(f) + r"\b") for f in NEW_FIELDS}

    for fpath in root.rglob("*"):
        if not fpath.is_file() or fpath.suffix not in CODE_EXTENSIONS:
            continue
        if _should_skip(fpath):
            continue

        analysis.files_scanned += 1
        try:
            text = fpath.read_text(errors="replace")
        except Exception:
            continue

        rel = str(fpath.relative_to(root))
        for i, line in enumerate(text.splitlines(), 1):
            for fname, pat in old_patterns.items():
                if pat.search(line):
                    analysis.old_refs.append(FieldRef(
                        file=rel, line_num=i,
                        line_text=line.strip()[:120], field_name=fname,
                    ))
            for fname, pat in new_patterns.items():
                if pat.search(line):
                    analysis.new_refs.append(FieldRef(
                        file=rel, line_num=i,
                        line_text=line.strip()[:120], field_name=fname,
                    ))

    return analysis


def detect_drift(config: CascadeConfig) -> DriftReport:
    """Compare field usage across all repos to detect schema drift."""
    analyses = [scan_repo(r) for r in config.repos]

    source = next((a for a in analyses if a.role == "source"), None)
    consumers = [a for a in analyses if a.role != "source"]

    if not source:
        return DriftReport(
            status="in_sync", analyses=analyses,
            change_summary="No source repo configured",
        )

    source_updated = source.has_new and not source.has_old

    if not source_updated:
        return DriftReport(
            status="in_sync", analyses=analyses,
            change_summary="All repositories are using the same schema. No drift detected.",
        )

    affected = [c for c in consumers if c.has_old]
    total_old = sum(len(c.old_refs) for c in affected)

    if not affected:
        return DriftReport(
            status="in_sync", analyses=analyses,
            change_summary="Source updated and all consumers are already in sync.",
        )

    affected_names = ", ".join(c.repo_name for c in affected)
    return DriftReport(
        status="drift_detected",
        analyses=analyses,
        change_summary=(
            f"The backend API has been updated to use full_name and author_name, "
            f"but {len(affected)} consumer repo(s) ({affected_names}) still "
            f"reference the old first_name/last_name fields ({total_old} references)."
        ),
        affected_count=len(affected),
        total_old_refs=total_old,
    )
