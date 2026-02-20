"""
Cascade Dashboard -- FastAPI + WebSocket for live pipeline monitoring.

Endpoints:
  GET  /                  Dashboard UI
  GET  /api/health        Health check
  GET  /api/detect        Scan repos for schema drift
  POST /api/simulate      Apply simulated backend change (for demo)
  POST /api/reset         Revert simulated change (for demo)
  POST /api/run           Trigger propagation
  GET  /api/status        Current run status
  POST /api/github/import Clone repos from GitHub
  POST /api/github/prs    Create PRs for propagated changes
  GET  /api/github/status GitHub integration status
  WS   /ws                Live event stream
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from ..core.cline import ClineWrapper
from ..core.config import CascadeConfig, RepoConfig, Settings, load_config
from ..core.detector import detect_drift
from ..core.github_ops import (
    CloneResult,
    clone_repo,
    create_pr,
    detect_language,
    detect_test_cmd,
    get_repo_default_branch,
    parse_github_url,
    push_branch,
)
from ..core.propagator import CascadeResult, Propagator

# Updated backend files for the "simulate" feature
BACKEND_V2_MODELS = '''\
"""Data models for the cross-platform demo API (v2 -- full_name schema)."""

from pydantic import BaseModel


class User(BaseModel):
    id: int
    full_name: str
    email: str
    role: str = "member"


class UserCreate(BaseModel):
    full_name: str
    email: str
    role: str = "member"


class Post(BaseModel):
    id: int
    author_name: str
    title: str
    body: str


USERS_DB: list[dict] = [
    {"id": 1, "full_name": "Alice Johnson", "email": "alice@example.com", "role": "admin"},
    {"id": 2, "full_name": "Bob Smith", "email": "bob@example.com", "role": "member"},
    {"id": 3, "full_name": "Carol Williams", "email": "carol@example.com", "role": "member"},
]

POSTS_DB: list[dict] = [
    {"id": 1, "author_name": "Alice Johnson", "title": "Welcome", "body": "Hello everyone!"},
    {"id": 2, "author_name": "Bob Smith", "title": "Update", "body": "New features shipped."},
]
'''

BACKEND_V2_MAIN = '''\
"""FastAPI backend for the cross-platform demo application (v2 -- full_name schema)."""

from fastapi import FastAPI, HTTPException

from models import POSTS_DB, USERS_DB, Post, User, UserCreate

app = FastAPI(title="Demo API", version="2.0.0")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/users", response_model=list[User])
def list_users():
    return [User(**u) for u in USERS_DB]


@app.get("/users/{user_id}", response_model=User)
def get_user(user_id: int):
    for u in USERS_DB:
        if u["id"] == user_id:
            return User(**u)
    raise HTTPException(status_code=404, detail="User not found")


@app.post("/users", response_model=User, status_code=201)
def create_user(payload: UserCreate):
    new_id = max(u["id"] for u in USERS_DB) + 1
    user_data = {"id": new_id, **payload.model_dump()}
    USERS_DB.append(user_data)
    return User(**user_data)


@app.get("/posts", response_model=list[Post])
def list_posts():
    return [Post(**p) for p in POSTS_DB]


@app.get("/users/{user_id}/display-name")
def get_display_name(user_id: int):
    for u in USERS_DB:
        if u["id"] == user_id:
            return {"display_name": u["full_name"]}
    raise HTTPException(status_code=404, detail="User not found")
'''

BACKEND_V2_TESTS = '''\
"""Tests for the demo backend API (v2 -- full_name schema)."""

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_list_users():
    resp = client.get("/users")
    assert resp.status_code == 200
    users = resp.json()
    assert len(users) >= 3
    assert "full_name" in users[0]


def test_get_user():
    resp = client.get("/users/1")
    assert resp.status_code == 200
    user = resp.json()
    assert user["full_name"] == "Alice Johnson"
    assert user["email"] == "alice@example.com"


def test_get_user_not_found():
    resp = client.get("/users/999")
    assert resp.status_code == 404


def test_create_user():
    resp = client.post("/users", json={
        "full_name": "Dave Brown",
        "email": "dave@example.com",
    })
    assert resp.status_code == 201
    user = resp.json()
    assert user["full_name"] == "Dave Brown"


def test_list_posts():
    resp = client.get("/posts")
    assert resp.status_code == 200
    posts = resp.json()
    assert len(posts) >= 2
    assert "author_name" in posts[0]


def test_display_name():
    resp = client.get("/users/1/display-name")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alice Johnson"
'''


class Broadcaster:
    def __init__(self):
        self.clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def send(self, message: dict):
        dead: list[WebSocket] = []
        for ws in self.clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.remove(ws)


async def _git(repo_path: str, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, out


def create_app(config_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Cascade Dashboard", version="0.2.0")
    bc = Broadcaster()

    run_history: list[dict] = []
    state: dict[str, Any] = {"current_run": None, "running": False}

    async def emit(event_type: str, data: dict):
        await bc.send({"type": event_type, "data": data, "ts": time.time()})

    def _get_config():
        if not config_path:
            raise ValueError("No config path")
        return load_config(config_path)

    # ── Pages ──────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html_path = Path(__file__).parent / "templates" / "index.html"
        if html_path.exists():
            return HTMLResponse(
                content=html_path.read_text(),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
        return HTMLResponse("<h1>Cascade Dashboard</h1><p>Template not found.</p>")

    # ── API ────────────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "running": state["running"]}

    @app.get("/api/detect")
    async def api_detect():
        try:
            cfg = _get_config()
            report = detect_drift(cfg)
            return report.to_dict()
        except Exception as exc:
            return {"status": "error", "change_summary": str(exc), "repos": []}

    @app.post("/api/simulate")
    async def api_simulate():
        """Apply the demo schema change to the backend-api repo."""
        try:
            cfg = _get_config()
            source = next((r for r in cfg.repos if r.role == "source"), None)
            if not source:
                return {"error": "No source repo found"}

            repo_dir = str(source.resolved_path)

            # Tag current state for reset
            await _git(repo_dir, "tag", "-f", "cascade-original")

            # Write updated files
            (source.resolved_path / "models.py").write_text(BACKEND_V2_MODELS)
            (source.resolved_path / "main.py").write_text(BACKEND_V2_MAIN)
            (source.resolved_path / "test_api.py").write_text(BACKEND_V2_TESTS)

            # Stage and commit
            await _git(repo_dir, "add", "-A")
            ret, _ = await _git(
                repo_dir, "commit", "-m",
                "API v2: full_name replaces first_name/last_name",
            )

            # Re-run detection
            report = detect_drift(cfg)
            return {
                "success": True,
                "message": "Backend API updated to v2 schema (full_name)",
                "detection": report.to_dict(),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @app.post("/api/reset")
    async def api_reset():
        """Reset all demo repos to their initial state."""
        try:
            cfg = _get_config()
            results = []

            for repo in cfg.repos:
                repo_dir = str(repo.resolved_path)
                branch_name = f"{cfg.settings.branch_prefix}{repo.name}"

                # Get default branch
                _, current = await _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")

                # If on a cascade branch, go back to main branch
                if current.startswith(cfg.settings.branch_prefix):
                    _, branches = await _git(repo_dir, "branch", "--format=%(refname:short)")
                    main_branch = "main" if "main" in branches else "master"
                    await _git(repo_dir, "checkout", main_branch)

                # Delete cascade branches
                await _git(repo_dir, "branch", "-D", branch_name)

                # For source repo: restore original files from tag
                if repo.role == "source":
                    ret, _ = await _git(repo_dir, "tag", "-l", "cascade-original")
                    if ret == 0:
                        await _git(repo_dir, "checkout", "cascade-original", "--", ".")
                        await _git(repo_dir, "add", "-A")
                        await _git(
                            repo_dir, "commit", "-m",
                            "Reset to original schema",
                        )

                results.append({"repo": repo.name, "status": "reset"})

            report = detect_drift(cfg)
            return {
                "success": True,
                "message": "All repos reset to initial state",
                "repos": results,
                "detection": report.to_dict(),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @app.get("/api/status")
    async def api_status():
        return {
            "current_run": state["current_run"],
            "history_count": len(run_history),
            "running": state["running"],
        }

    @app.get("/api/history")
    async def api_history():
        return {"runs": run_history[-20:]}

    @app.post("/api/run")
    async def api_run(change: str, config: Optional[str] = None):
        if state["running"]:
            return {"error": "A run is already in progress"}

        cfg_path = config or config_path
        if not cfg_path:
            return {"error": "No config path provided"}

        cfg = load_config(cfg_path)
        cline = ClineWrapper(
            max_concurrent=cfg.settings.max_parallel,
            default_timeout=cfg.settings.timeout_per_repo,
        )

        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        adapt_tpl = _load_tpl(prompts_dir / "adapt.md")
        verify_tpl = _load_tpl(prompts_dir / "verify.md")
        fix_tpl = _load_tpl(prompts_dir / "fix_tests.md")

        state["running"] = True

        async def _run_in_background():
            try:
                propagator = Propagator(
                    config=cfg,
                    cline=cline,
                    on_event=emit,
                    adapt_prompt_template=adapt_tpl,
                    verify_prompt_template=verify_tpl,
                    fix_prompt_template=fix_tpl,
                )
                result = await propagator.run(change)
                result_dict = result.to_dict()
                state["current_run"] = result_dict
                run_history.append(result_dict)
                await emit("cascade.completed", result_dict)
            finally:
                state["running"] = False

        asyncio.create_task(_run_in_background())
        return {"status": "started", "change": change}

    # ── GitHub integration ─────────────────────────────────

    WORKSPACE_DIR = Path(__file__).resolve().parent.parent / "workspace"
    gh_state: dict[str, Any] = {
        "repos": [],       # list of dicts: {name, github, path, role, language, ...}
        "cloning": False,
        "prs": [],         # list of PRResult dicts
        "config": None,    # CascadeConfig built from GitHub repos
    }

    @app.post("/api/github/import")
    async def api_github_import(
        repos: list[str],
        source_repo: str = "",
    ):
        """Clone GitHub repos, detect languages, build dynamic config."""
        if gh_state["cloning"]:
            return {"error": "Clone already in progress"}

        gh_state["cloning"] = True
        gh_state["repos"] = []
        gh_state["prs"] = []
        results: list[dict] = []

        try:
            WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

            for url in repos:
                url = url.strip()
                if not url:
                    continue

                await emit("github.cloning", {"repo": url})
                cr = await clone_repo(url, WORKSPACE_DIR, depth=0)

                if not cr.success:
                    results.append({
                        "name": cr.name,
                        "github": url,
                        "status": "failed",
                        "error": cr.error,
                    })
                    continue

                lang = await detect_language(cr.local_path)
                test_cmd = await detect_test_cmd(cr.local_path, lang)
                owner, _ = parse_github_url(url)
                is_source = (
                    cr.name == source_repo
                    or url == source_repo
                    or f"{owner}/{cr.name}" == source_repo
                )

                repo_info = {
                    "name": cr.name,
                    "github": url,
                    "path": cr.local_path,
                    "role": "source" if is_source else "consumer",
                    "language": lang,
                    "test_cmd": test_cmd,
                    "default_branch": cr.default_branch,
                    "status": "cloned",
                }
                gh_state["repos"].append(repo_info)
                results.append(repo_info)
                await emit("github.cloned", repo_info)

            repo_configs = [
                RepoConfig(
                    name=r["name"],
                    path=r["path"],
                    role=r["role"],
                    language=r["language"],
                    test_cmd=r.get("test_cmd", ""),
                    github=r["github"],
                )
                for r in gh_state["repos"]
            ]
            gh_state["config"] = CascadeConfig(
                name="github-import",
                repos=repo_configs,
                settings=Settings(),
            )

            try:
                report = detect_drift(gh_state["config"])
                detection = report.to_dict()
            except Exception:
                detection = None

            return {
                "success": True,
                "repos": results,
                "detection": detection,
                "workspace": str(WORKSPACE_DIR),
            }
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            gh_state["cloning"] = False

    @app.post("/api/github/detect")
    async def api_github_detect():
        """Run drift detection on GitHub-imported repos."""
        if not gh_state["config"]:
            return {"error": "No GitHub repos imported. Use /api/github/import first."}

        try:
            report = detect_drift(gh_state["config"])
            return report.to_dict()
        except Exception as exc:
            return {"status": "error", "change_summary": str(exc), "repos": []}

    @app.post("/api/github/run")
    async def api_github_run(change: str):
        """Run propagation on GitHub-imported repos, then push + create PRs."""
        if state["running"]:
            return {"error": "A run is already in progress"}
        if not gh_state["config"]:
            return {"error": "No GitHub repos imported. Use /api/github/import first."}

        cfg = gh_state["config"]
        cline = ClineWrapper(
            max_concurrent=cfg.settings.max_parallel,
            default_timeout=cfg.settings.timeout_per_repo,
        )

        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
        adapt_tpl = _load_tpl(prompts_dir / "adapt.md")
        verify_tpl = _load_tpl(prompts_dir / "verify.md")
        fix_tpl = _load_tpl(prompts_dir / "fix_tests.md")

        state["running"] = True

        async def _run_github():
            try:
                propagator = Propagator(
                    config=cfg,
                    cline=cline,
                    on_event=emit,
                    adapt_prompt_template=adapt_tpl,
                    verify_prompt_template=verify_tpl,
                    fix_prompt_template=fix_tpl,
                )
                result = await propagator.run(change)
                result_dict = result.to_dict()
                state["current_run"] = result_dict
                run_history.append(result_dict)
                await emit("cascade.completed", result_dict)
            finally:
                state["running"] = False

        asyncio.create_task(_run_github())
        return {"status": "started", "change": change}

    @app.post("/api/github/prs")
    async def api_github_create_prs():
        """Push cascade branches and create PRs for all propagated repos."""
        if not gh_state["repos"]:
            return {"error": "No GitHub repos imported"}
        if not state.get("current_run"):
            return {"error": "No propagation run to create PRs from"}

        run = state["current_run"]
        pr_results: list[dict] = []

        for repo_run in run.get("repos", []):
            if repo_run["status"] != "done":
                continue

            repo_name = repo_run["repo_name"]
            repo_info = next(
                (r for r in gh_state["repos"] if r["name"] == repo_name), None,
            )
            if not repo_info or repo_info["role"] == "source":
                continue

            branch = repo_run.get("branch", "")
            if not branch:
                continue

            repo_dir = repo_info["path"]
            default_branch = repo_info.get("default_branch", "main")

            await emit("github.pushing", {"repo": repo_name, "branch": branch})

            _, current = await _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
            if current != branch:
                await _git(repo_dir, "checkout", branch)

            ok, push_err = await push_branch(repo_dir, branch)
            if not ok:
                pr_results.append({
                    "repo_name": repo_name,
                    "branch": branch,
                    "success": False,
                    "error": f"Push failed: {push_err}",
                    "pr_url": "",
                })
                await _git(repo_dir, "checkout", default_branch)
                continue

            await emit("github.creating_pr", {"repo": repo_name, "branch": branch})

            change_desc = run.get("change_description", "schema change")
            pr_body = (
                f"## Cascade Auto-Propagation\n\n"
                f"**Change:** {change_desc}\n\n"
                f"**Files changed:** {len(repo_run.get('files_changed', []))}\n"
                f"**Tests:** {'Passed' if repo_run.get('test_passed') else 'Not run'}\n\n"
                f"```\n{repo_run.get('diff_stat', '')}\n```\n\n"
                f"---\n*Created by Cascade using [Cline CLI](https://cline.bot) "
                f"as infrastructure.*"
            )

            pr_res = await create_pr(
                repo_dir,
                title=f"cascade: {change_desc[:60]}",
                body=pr_body,
                base=default_branch,
                head=branch,
            )

            pr_dict = pr_res.to_dict()
            pr_results.append(pr_dict)
            await emit("github.pr_created", pr_dict)

            await _git(repo_dir, "checkout", default_branch)

        gh_state["prs"] = pr_results
        return {"success": True, "prs": pr_results}

    @app.get("/api/github/status")
    async def api_github_status():
        """Current state of the GitHub integration."""
        return {
            "repos": gh_state["repos"],
            "prs": gh_state["prs"],
            "cloning": gh_state["cloning"],
            "has_config": gh_state["config"] is not None,
        }

    @app.post("/api/github/update-role")
    async def api_github_update_role(repo_name: str, role: str):
        """Update the role of an imported GitHub repo (source/consumer)."""
        for r in gh_state["repos"]:
            if r["name"] == repo_name:
                r["role"] = role
                break
        else:
            return {"error": f"Repo '{repo_name}' not found"}

        if gh_state["config"]:
            for rc in gh_state["config"].repos:
                if rc.name == repo_name:
                    rc.role = role
                    break

        return {"success": True, "repo": repo_name, "role": role}

    # ── WebSocket ──────────────────────────────────────────

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await bc.connect(ws)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            bc.disconnect(ws)

    return app


def _load_tpl(path: Path) -> Optional[str]:
    return path.read_text() if path.exists() else None
