# Cascade -- Multi-Repo Change Propagator

**Cline CLI as Infrastructure** | DevWeek Hackathon 2026

> One API change. Multiple repos. Zero manual hunting.

Cascade takes a single change description and propagates it across all connected repositories using the real [Cline CLI](https://docs.cline.bot/cline-cli/overview) as parallel worker agents. Each repo gets its own branch with adapted code, passing tests, and a self-reviewed diff.

## The Problem

In cross-platform applications, a single API change (rename a field, add a parameter, change a response shape) ripples across every consumer -- web frontend, SDK, CLI tool, docs. Engineers manually hunt down every reference in every repo, adapt each one for its language and framework, and hope they didn't miss anything. This takes hours and is error-prone.

## The Solution

```
"The /users endpoint now returns full_name instead of first_name and last_name"
     |
     v
  Cascade
     |
     +---> [cline -y] Backend API      --> branch + adapted code
     +---> [cline -y] Web Dashboard    --> branch + adapted code
     +---> [cline -y] Python SDK       --> branch + adapted code
     +---> [cline -y] CLI Client       --> branch + adapted code
```

Each repo is handled by an independent Cline agent that:
1. **Adapts** the code using `cline -y -c <repo>` (headless, auto-approve)
2. **Tests** using the repo's configured test command
3. **Fixes** failures by piping test output back to `cline -y`
4. **Self-reviews** using `git diff | cline --json`
5. **Commits** to an isolated branch

## Cline CLI Usage

Every interaction uses documented flags from the [Cline CLI Reference](https://docs.cline.bot/cline-cli/cli-reference):

| Step     | Command                                                          | Purpose                         |
| -------- | ---------------------------------------------------------------- | ------------------------------- |
| Discover | `cline --json -c <repo> "List files affected by: <change>"`     | Find impacted files per repo    |
| Adapt    | `cline -y -c <repo> --timeout 600 "Apply this change: <change>"`| Implement adaptation headlessly |
| Verify   | `git diff \| cline --json "Review these changes"`               | Self-review the diff            |
| Fix      | `<test output> \| cline -y -c <repo> "Fix these failures"`      | Auto-fix if tests break         |

## Architecture

```
cascade/
  __init__.py, __main__.py
  cli.py                  # Typer CLI: run, status, dashboard, init
  core/
    cline.py              # Real Cline CLI subprocess wrapper
    config.py             # cascade.yaml loader
    discovery.py          # Discover affected files per repo
    propagator.py         # Parallel dispatch + pipeline orchestration
    git_ops.py            # Branch, commit, diff operations
    github_ops.py         # Clone, push, PR creation via gh CLI
    reporter.py           # Summary generation
  prompts/
    discover.md           # Discovery prompt template
    adapt.md              # Adaptation prompt template
    verify.md             # Self-review prompt template
    fix_tests.md          # Test-fix prompt template
  dashboard/
    app.py                # FastAPI + WebSocket server
    templates/
      index.html          # Live monitoring dashboard
demo/
  cascade.yaml            # Config pointing to demo repos
  run-demo.sh             # One-command demo script
  repos/
    backend-api/          # Python FastAPI backend
    web-dashboard/        # HTML/JS frontend
    python-sdk/           # Python SDK
    cli-client/           # Python CLI tool
```

## Quick Start

### Prerequisites

- **Node.js 18+** (for Cline CLI)
- **Python 3.11+**
- **Git**
- A configured Cline API provider

### Setup

```bash
# 1. Install Cline CLI
npm install -g cline

# 2. Authenticate Cline (follow prompts to set API key)
cline auth

# 3. Install Python dependencies
cd cline/
pip install -r requirements.txt

# 4. Run the demo
bash demo/run-demo.sh
```

### CLI Commands

```bash
# Propagate a change across all repos
python -m cascade run "The /users endpoint returns full_name instead of first_name and last_name"

# Use a specific config file
python -m cascade run --config ./demo/cascade.yaml "change description"

# Dry-run (discovery only, no changes)
python -m cascade run --dry-run "change description"

# Launch live dashboard
python -m cascade dashboard

# Show last run results
python -m cascade status

# Initialize a new cascade.yaml
python -m cascade init
```

### Docker

```bash
# Launch the dashboard
docker compose up cascade

# Run a propagation
docker compose run --rm cascade-run

# Or with a custom change
docker compose run --rm cascade-run run --config /app/demo/cascade.yaml "your change description"
```

## Demo Walkthrough

The included demo simulates a cross-platform application with 4 repos:

| Repo | Language | Role | Description |
|------|----------|------|-------------|
| `backend-api` | Python | Source | FastAPI backend with `/users`, `/posts` endpoints |
| `web-dashboard` | JavaScript | Consumer | HTML/JS frontend displaying user names |
| `python-sdk` | Python | Consumer | SDK wrapping the API with dataclasses |
| `cli-client` | Python | Consumer | CLI tool formatting user output |

All repos use `first_name` and `last_name` fields. The demo change is:

> **"The /users endpoint now returns `full_name` instead of separate `first_name` and `last_name` fields."**

Running `bash demo/run-demo.sh` will:

1. Initialize each demo repo as a git repository
2. Run baseline tests (all pass)
3. Launch Cascade to propagate the change across all 4 repos in parallel
4. Show a Rich live table with real-time status per repo
5. Print a summary of branches created, files changed, and test results

## Configuration

The `cascade.yaml` file defines your repos and settings:

```yaml
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
  max_parallel: 4          # Max concurrent Cline agents
  timeout_per_repo: 600    # Seconds per repo
  auto_branch: true        # Create branches automatically
  branch_prefix: "cascade/"
  retry_on_test_fail: true # Retry with test output on failure
  max_retries: 2
```

## Live Dashboard

Launch with `python -m cascade dashboard` to get a real-time web UI at `http://localhost:8450`:

- **Demo Mode** -- simulate schema changes on local repos with one click
- **GitHub Mode** -- import repos from GitHub, detect drift, propagate changes, and create PRs
- **Repo cards** showing status, branch, files changed, test results
- **Event log** with timestamped pipeline events
- **WebSocket** for instant updates
- **Light/dark theme** toggle with auto-persist

## GitHub Integration

Cascade can clone repositories directly from GitHub, detect schema drift, propagate changes using Cline CLI, and create pull requests automatically.

### Setup

```bash
# Authenticate GitHub CLI (required for PR creation)
gh auth login
```

### Usage (Dashboard)

1. Switch to **GitHub Repos** tab in the dashboard
2. Paste GitHub repository URLs (one per line, e.g. `owner/repo` or full URL)
3. Select the **source** (origin) repo
4. Click **Import & Scan** -- repos are cloned, language auto-detected, and drift analysis runs
5. If drift is detected, click **Propagate Changes** -- Cline agents adapt each consumer repo
6. Click **Create All PRs** -- branches are pushed and PRs are created on GitHub

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/github/import` | Clone repos from GitHub URLs |
| `POST` | `/api/github/detect` | Run drift detection on imported repos |
| `POST` | `/api/github/run` | Trigger propagation on GitHub repos |
| `POST` | `/api/github/prs` | Push branches and create PRs |
| `GET`  | `/api/github/status` | Current GitHub integration state |
| `POST` | `/api/github/update-role` | Change a repo's role (source/consumer) |

## License

MIT
