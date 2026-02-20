"""
Real Cline CLI wrapper -- invokes the cline binary via subprocess
using documented flags from https://docs.cline.bot/cline-cli/cli-reference.

Supported modes:
  - Headless:  cline -y "prompt"            (auto-approve, plain text)
  - JSON:      cline --json "prompt"        (structured output)
  - Piped:     echo data | cline -y "prompt"(stdin piped)
  - Plan:      cline --json -p "prompt"     (plan mode)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class ClineResult:
    """Outcome of a single Cline CLI invocation."""

    invocation_id: str = ""
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    success: bool = True
    duration_seconds: float = 0.0
    error: str = ""
    json_messages: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "exit_code": self.exit_code,
            "success": self.success,
            "duration_seconds": round(self.duration_seconds, 2),
            "error": self.error,
            "stdout_len": len(self.stdout),
            "json_message_count": len(self.json_messages),
        }

    @property
    def text_output(self) -> str:
        """Extract human-readable text from JSON messages or raw stdout."""
        if self.json_messages:
            parts = []
            for msg in self.json_messages:
                if msg.get("type") == "say" and msg.get("text"):
                    parts.append(msg["text"])
            return "\n".join(parts)
        return self.stdout


class ClineWrapper:
    """
    Thin async wrapper around the real Cline CLI binary.

    Maps directly to the documented CLI flags:
      -y / --yolo       → headless auto-approve
      --json            → machine-readable JSON output
      -p / --plan       → plan mode
      -a / --act        → act mode (default)
      -c / --cwd        → working directory
      -m / --model      → model selection
      --timeout         → execution timeout
    """

    def __init__(
        self,
        binary: Optional[str] = None,
        max_concurrent: int = 4,
        default_timeout: int = 600,
    ):
        self.binary = binary or self._locate_binary()
        self.max_concurrent = max_concurrent
        self.default_timeout = default_timeout
        self._sem = asyncio.Semaphore(max_concurrent)
        self._active: dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    def _locate_binary() -> str:
        found = shutil.which("cline")
        if found:
            return found
        return "cline"

    def _build_cmd(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        yolo: bool = False,
        json_output: bool = False,
        plan_mode: bool = False,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> list[str]:
        cmd = [self.binary]

        if yolo:
            cmd.append("-y")
        if json_output:
            cmd.append("--json")
        if plan_mode:
            cmd.append("-p")
        if cwd:
            cmd.extend(["-c", str(cwd)])
        if model:
            cmd.extend(["-m", model])
        if timeout:
            cmd.extend(["--timeout", str(timeout)])

        cmd.append(prompt)
        return cmd

    async def invoke(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        yolo: bool = False,
        json_output: bool = False,
        plan_mode: bool = False,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        stdin_data: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        on_output: Optional[Callable[[str], Any]] = None,
    ) -> ClineResult:
        """
        Run a single Cline CLI invocation.

        Args:
            prompt:      The task prompt.
            cwd:         Working directory (-c flag).
            yolo:        Auto-approve all actions (-y flag).
            json_output: Output as JSON (--json flag).
            plan_mode:   Start in plan mode (-p flag).
            model:       Model to use (-m flag).
            timeout:     Max execution time (--timeout flag).
            stdin_data:  Data to pipe to stdin.
            env:         Extra environment variables (e.g. CLINE_COMMAND_PERMISSIONS).
            on_output:   Callback for streaming output lines.
        """
        async with self._sem:
            return await self._run(
                prompt, cwd, yolo, json_output, plan_mode,
                model, timeout, stdin_data, env, on_output,
            )

    async def _run(
        self,
        prompt: str,
        cwd: Optional[str],
        yolo: bool,
        json_output: bool,
        plan_mode: bool,
        model: Optional[str],
        timeout: Optional[int],
        stdin_data: Optional[str],
        env: Optional[dict[str, str]],
        on_output: Optional[Callable],
    ) -> ClineResult:
        import os

        inv_id = uuid.uuid4().hex[:10]
        effective_timeout = timeout or self.default_timeout
        cmd = self._build_cmd(prompt, cwd, yolo, json_output, plan_mode, model, timeout)
        result = ClineResult(invocation_id=inv_id)
        start = time.monotonic()

        proc_env = {**os.environ}
        if env:
            proc_env.update(env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_data else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )
            self._active[inv_id] = proc

            out_chunks: list[str] = []
            err_chunks: list[str] = []

            async def read_stream(stream, chunks, callback=None):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace")
                    chunks.append(text)
                    if callback:
                        cb_result = callback(text)
                        if asyncio.iscoroutine(cb_result):
                            await cb_result

            try:
                if stdin_data:
                    proc.stdin.write(stdin_data.encode())
                    await proc.stdin.drain()
                    proc.stdin.close()

                await asyncio.wait_for(
                    asyncio.gather(
                        read_stream(proc.stdout, out_chunks, on_output),
                        read_stream(proc.stderr, err_chunks),
                    ),
                    timeout=effective_timeout,
                )
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                result.error = f"Timed out after {effective_timeout}s"

            result.stdout = "".join(out_chunks)
            result.stderr = "".join(err_chunks)
            result.exit_code = proc.returncode or 0
            result.success = result.exit_code == 0 and not result.error

            if json_output and result.stdout:
                result.json_messages = self._parse_json_lines(result.stdout)

        except FileNotFoundError:
            result.success = False
            result.exit_code = 127
            result.error = (
                f"Cline CLI not found ('{self.binary}'). "
                "Install with: npm install -g cline"
            )
        except Exception as exc:
            result.success = False
            result.exit_code = 1
            result.error = str(exc)
        finally:
            self._active.pop(inv_id, None)
            result.duration_seconds = round(time.monotonic() - start, 2)

        return result

    async def cancel(self, invocation_id: str) -> bool:
        proc = self._active.get(invocation_id)
        if proc:
            proc.kill()
            await proc.wait()
            self._active.pop(invocation_id, None)
            return True
        return False

    async def cancel_all(self):
        for inv_id in list(self._active):
            await self.cancel(inv_id)

    @staticmethod
    def _parse_json_lines(output: str) -> list[dict]:
        """Parse --json output: one JSON object per line."""
        messages = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return messages

    @staticmethod
    def build_permissions(
        allow: Optional[list[str]] = None,
        deny: Optional[list[str]] = None,
    ) -> str:
        """Build a CLINE_COMMAND_PERMISSIONS JSON string."""
        perms: dict[str, Any] = {}
        if allow:
            perms["allow"] = allow
        if deny:
            perms["deny"] = deny
        return json.dumps(perms)
