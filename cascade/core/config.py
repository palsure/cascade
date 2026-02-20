"""Configuration loader for Cascade YAML files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class RepoConfig:
    name: str
    path: str
    role: str = "consumer"
    language: str = "unknown"
    test_cmd: str = ""
    github: str = ""

    @property
    def resolved_path(self) -> Path:
        return Path(self.path).resolve()

    @property
    def is_source(self) -> bool:
        return self.role == "source"

    @property
    def is_github(self) -> bool:
        return bool(self.github)


@dataclass
class Settings:
    max_parallel: int = 4
    timeout_per_repo: int = 600
    auto_branch: bool = True
    branch_prefix: str = "cascade/"
    retry_on_test_fail: bool = True
    max_retries: int = 2
    model: str = ""


@dataclass
class CascadeConfig:
    name: str
    repos: list[RepoConfig] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    config_dir: Path = field(default_factory=lambda: Path.cwd())

    @property
    def source_repos(self) -> list[RepoConfig]:
        return [r for r in self.repos if r.is_source]

    @property
    def consumer_repos(self) -> list[RepoConfig]:
        return [r for r in self.repos if not r.is_source]

    @property
    def all_repos(self) -> list[RepoConfig]:
        return self.repos


def load_config(path: str | Path) -> CascadeConfig:
    """Load a cascade.yaml configuration file."""
    config_path = Path(path).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid config format in {config_path}")

    config_dir = config_path.parent

    repos = []
    for r in raw.get("repos", []):
        repo_path = r.get("path", ".")
        if not Path(repo_path).is_absolute():
            repo_path = str(config_dir / repo_path)
        repos.append(RepoConfig(
            name=r["name"],
            path=repo_path,
            role=r.get("role", "consumer"),
            language=r.get("language", "unknown"),
            test_cmd=r.get("test_cmd", ""),
            github=r.get("github", ""),
        ))

    settings_raw = raw.get("settings", {})
    settings = Settings(
        max_parallel=settings_raw.get("max_parallel", 4),
        timeout_per_repo=settings_raw.get("timeout_per_repo", 600),
        auto_branch=settings_raw.get("auto_branch", True),
        branch_prefix=settings_raw.get("branch_prefix", "cascade/"),
        retry_on_test_fail=settings_raw.get("retry_on_test_fail", True),
        max_retries=settings_raw.get("max_retries", 2),
        model=settings_raw.get("model", ""),
    )

    return CascadeConfig(
        name=raw.get("name", "unnamed"),
        repos=repos,
        settings=settings,
        config_dir=config_dir,
    )
