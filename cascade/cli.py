"""
Cascade CLI -- powered by Typer + Rich.

Commands:
  cascade run "change description"   Propagate a change across all repos
  cascade dashboard                  Launch the live monitoring dashboard
  cascade status                     Show the last run results
  cascade init                       Initialize a new cascade.yaml
  cascade version                    Show version info
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .core.cline import ClineWrapper
from .core.config import load_config
from .core.propagator import CascadeResult, Propagator, Status
from .core.reporter import generate_summary

app = typer.Typer(
    name="cascade",
    help="Multi-repo change propagator powered by Cline CLI as infrastructure.",
    no_args_is_help=True,
)
console = Console()

BANNER = r"""
   ____                        _
  / ___|__ _ ___  ___ __ _  __| | ___
 | |   / _` / __|/ __/ _` |/ _` |/ _ \
 | |__| (_| \__ \ (_| (_| | (_| |  __/
  \____\__,_|___/\___\__,_|\__,_|\___|
  Multi-Repo Change Propagator · Cline CLI as Infrastructure
"""

STATE_FILE = Path.home() / ".cascade" / "last_run.json"


def _find_config(config_path: Optional[str]) -> Path:
    if config_path:
        return Path(config_path)
    candidates = [
        Path.cwd() / "cascade.yaml",
        Path.cwd() / "cascade.yml",
        Path.cwd() / "demo" / "cascade.yaml",
    ]
    for c in candidates:
        if c.exists():
            return c
    console.print("[red]No cascade.yaml found.[/red] Run 'cascade init' or pass --config.")
    raise typer.Exit(1)


def _save_result(result: CascadeResult):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(result.to_dict(), indent=2, default=str))


def _build_live_table(result: CascadeResult) -> Table:
    table = Table(title="Cascade Propagation", show_lines=True)
    table.add_column("Repo", style="cyan", min_width=16)
    table.add_column("Language", style="dim")
    table.add_column("Status", min_width=12)
    table.add_column("Branch", style="dim")
    table.add_column("Tests", min_width=8)
    table.add_column("Files", justify="right")
    table.add_column("Time", justify="right")

    status_styles = {
        Status.WAITING: "[dim]waiting[/dim]",
        Status.BRANCHING: "[yellow]branching[/yellow]",
        Status.ADAPTING: "[blue bold]adapting...[/blue bold]",
        Status.TESTING: "[yellow]testing[/yellow]",
        Status.FIXING: "[magenta]fixing...[/magenta]",
        Status.REVIEWING: "[cyan]reviewing[/cyan]",
        Status.COMMITTING: "[yellow]committing[/yellow]",
        Status.DONE: "[green bold]done[/green bold]",
        Status.FAILED: "[red bold]FAILED[/red bold]",
        Status.SKIPPED: "[dim]skipped[/dim]",
    }

    for repo in result.repo_results:
        status_text = status_styles.get(repo.status, repo.status)
        test_text = "[green]pass[/green]" if repo.test_passed else "[dim]-[/dim]"
        if repo.status == Status.FAILED and repo.test_output:
            test_text = "[red]fail[/red]"
        files = str(len(repo.files_changed)) if repo.files_changed else "-"
        dur = f"{repo.duration}s" if repo.started_at else "-"

        table.add_row(
            repo.repo_name,
            repo.language,
            status_text,
            repo.branch or "-",
            test_text,
            files,
            dur,
        )

    return table


@app.command()
def run(
    change: str = typer.Argument(..., help="Description of the API/schema change to propagate"),
    config: Optional[str] = typer.Option(None, "--config", "-f", help="Path to cascade.yaml"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Discovery only, no changes"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override Cline model"),
):
    """Propagate a change across all configured repositories."""
    console.print(BANNER, style="bold cyan")
    console.print(f"[bold]Change:[/bold] {change}\n")

    config_path = _find_config(config)
    cfg = load_config(config_path)
    console.print(f"[dim]Config: {config_path}[/dim]")
    console.print(f"[dim]Repos:  {len(cfg.repos)}[/dim]\n")

    if model:
        cfg.settings.model = model

    # Load prompt templates
    prompts_dir = Path(__file__).resolve().parent / "prompts"
    adapt_tpl = _load_template(prompts_dir / "adapt.md")
    verify_tpl = _load_template(prompts_dir / "verify.md")
    fix_tpl = _load_template(prompts_dir / "fix_tests.md")

    cline = ClineWrapper(
        max_concurrent=cfg.settings.max_parallel,
        default_timeout=cfg.settings.timeout_per_repo,
    )

    cascade_result = CascadeResult(change_description=change, started_at=time.time())

    async def _run():
        propagator = Propagator(
            config=cfg,
            cline=cline,
            adapt_prompt_template=adapt_tpl,
            verify_prompt_template=verify_tpl,
            fix_prompt_template=fix_tpl,
        )
        return await propagator.run(change, dry_run=dry_run)

    with Live(_build_live_table(cascade_result), console=console, refresh_per_second=2) as live:
        async def _run_with_live():
            propagator = Propagator(
                config=cfg,
                cline=cline,
                adapt_prompt_template=adapt_tpl,
                verify_prompt_template=verify_tpl,
                fix_prompt_template=fix_tpl,
                on_event=lambda t, d: _update_live(live, cascade_result),
            )
            nonlocal cascade_result
            cascade_result = await propagator.run(change, dry_run=dry_run)
            live.update(_build_live_table(cascade_result))

        asyncio.run(_run_with_live())

    console.print()
    console.print(generate_summary(cascade_result))

    _save_result(cascade_result)

    if cascade_result.fail_count > 0:
        raise typer.Exit(1)


def _update_live(live: Live, result: CascadeResult):
    live.update(_build_live_table(result))


@app.command()
def status():
    """Show the last run results."""
    if not STATE_FILE.exists():
        console.print("[dim]No previous run found.[/dim]")
        raise typer.Exit(0)

    data = json.loads(STATE_FILE.read_text())
    console.print(Panel(
        json.dumps(data, indent=2),
        title="Last Cascade Run",
        border_style="cyan",
    ))


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", help="Dashboard host"),
    port: int = typer.Option(8450, help="Dashboard port"),
    config: Optional[str] = typer.Option(None, "--config", "-f", help="Path to cascade.yaml"),
):
    """Launch the live monitoring dashboard."""
    console.print(BANNER, style="bold cyan")
    console.print(f"Starting dashboard at http://{host}:{port}\n")

    import uvicorn
    from .dashboard.app import create_app

    config_path = str(_find_config(config)) if config else None
    dash_app = create_app(config_path=config_path)
    uvicorn.run(dash_app, host=host, port=port, log_level="info")


@app.command()
def init(
    path: str = typer.Option(".", help="Directory to create cascade.yaml in"),
):
    """Initialize a new cascade.yaml configuration."""
    target = Path(path) / "cascade.yaml"
    if target.exists():
        console.print(f"[yellow]{target} already exists.[/yellow]")
        raise typer.Exit(1)

    template = """\
name: my-project

repos:
  - name: backend
    path: ./backend
    role: source
    language: python
    test_cmd: "python -m pytest -v"

  - name: frontend
    path: ./frontend
    role: consumer
    language: javascript
    test_cmd: "npm test"

settings:
  max_parallel: 4
  timeout_per_repo: 600
  auto_branch: true
  branch_prefix: "cascade/"
  retry_on_test_fail: true
  max_retries: 2
"""
    target.write_text(template)
    console.print(f"[green]Created {target}[/green]")


@app.command()
def version():
    """Show Cascade version."""
    console.print(f"Cascade v{__version__}")

    import shutil
    cline_bin = shutil.which("cline")
    if cline_bin:
        console.print(f"Cline CLI: {cline_bin}")
    else:
        console.print("[yellow]Cline CLI: not found (install with npm install -g cline)[/yellow]")


def _load_template(path: Path) -> Optional[str]:
    if path.exists():
        return path.read_text()
    return None
