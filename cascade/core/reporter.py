"""Summary report generation for Cascade runs."""

from __future__ import annotations

from .propagator import CascadeResult, Status


def generate_summary(result: CascadeResult) -> str:
    lines = [
        "=" * 60,
        "  CASCADE PROPAGATION SUMMARY",
        "=" * 60,
        "",
        f"  Change: {result.change_description[:80]}",
        f"  Duration: {result.duration}s",
        f"  Repos: {len(result.repo_results)} total, "
        f"{result.success_count} succeeded, {result.fail_count} failed",
        "",
        "-" * 60,
    ]

    for repo in result.repo_results:
        icon = "OK" if repo.success else "SKIP" if repo.status == Status.SKIPPED else "FAIL"
        lines.append(f"  [{icon:>4}] {repo.repo_name} ({repo.language})")
        if repo.branch:
            lines.append(f"         Branch: {repo.branch}")
        if repo.files_changed:
            lines.append(f"         Files changed: {len(repo.files_changed)}")
            for f in repo.files_changed[:5]:
                lines.append(f"           - {f}")
            if len(repo.files_changed) > 5:
                lines.append(f"           ... and {len(repo.files_changed) - 5} more")
        if repo.test_passed:
            lines.append(f"         Tests: passed")
        elif repo.test_output:
            lines.append(f"         Tests: FAILED (retries: {repo.retries_used})")
        if repo.error:
            lines.append(f"         Error: {repo.error[:80]}")
        if repo.review_summary:
            preview = repo.review_summary.replace("\n", " ")[:100]
            lines.append(f"         Review: {preview}")
        lines.append(f"         Duration: {repo.duration}s")
        lines.append("")

    lines.append("=" * 60)
    return "\n".join(lines)
