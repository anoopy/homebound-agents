"""GitHub tracker implementation.

Implements the Tracker ABC for GitHub Issues/PRs via the `gh` CLI.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from pathlib import Path

from homebound.adapters.tracker import (
    ClassifiedCommand,
    CommandLevel,
    Tracker,
    TrackerResult,
)

logger = logging.getLogger("homebound.tracker.github")


class GitHubTracker(Tracker):
    """Proxies structured text commands to the local `gh` CLI."""

    def __init__(self, project_dir: Path, command_timeout: int = 30) -> None:
        self.project_dir = project_dir
        self.command_timeout = command_timeout

    @classmethod
    def from_config(cls, tracker_config) -> GitHubTracker:
        """Create a GitHubTracker from TrackerConfig."""
        return cls(
            project_dir=Path(tracker_config.project_dir).resolve(),
            command_timeout=tracker_config.command_timeout,
        )

    def classify(self, command_text: str) -> ClassifiedCommand | None:
        """Classify a command without executing it."""
        cmd = command_text.strip()
        cmd_lower = cmd.lower()

        # new <title> [// <body>]  (create issue)
        m = re.match(r"new\s+(.+)", cmd, re.IGNORECASE)
        if m:
            raw = m.group(1)
            if "//" in raw:
                title, body = raw.split("//", 1)
                return ClassifiedCommand(
                    handler="_create_issue",
                    args=(title.strip(), body.strip()),
                    level=CommandLevel.WRITE,
                    description=f"Create issue: {title.strip()[:50]}",
                )
            return ClassifiedCommand(
                handler="_create_issue",
                args=(raw.strip(),),
                level=CommandLevel.WRITE,
                description=f"Create issue: {raw.strip()[:50]}",
            )

        # ls pr  (list PRs — must come before bare "ls")
        if cmd_lower == "ls pr":
            return ClassifiedCommand(
                handler="_pr_list",
                args=(),
                level=CommandLevel.READ,
                description="List open PRs",
            )

        # ls  (list issues)
        if cmd_lower == "ls":
            return ClassifiedCommand(
                handler="_list_issues",
                args=(),
                level=CommandLevel.READ,
                description="List open issues",
            )

        # rm <N>  (close issue — DESTRUCTIVE)
        m = re.match(r"rm\s+(\d+)$", cmd, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return ClassifiedCommand(
                handler="_close_issue",
                args=(n,),
                level=CommandLevel.DESTRUCTIVE,
                description=f"Close issue #{n}",
            )

        # view pr <N>  (view PR — must come before bare "view <N>")
        m = re.match(r"view\s+pr\s+(\d+)$", cmd, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return ClassifiedCommand(
                handler="_pr_view",
                args=(n,),
                level=CommandLevel.READ,
                description=f"View PR #{n}",
            )

        # view <N>  (view issue)
        m = re.match(r"view\s+(\d+)$", cmd, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            return ClassifiedCommand(
                handler="_view_issue",
                args=(n,),
                level=CommandLevel.READ,
                description=f"View issue #{n}",
            )

        # echo <N> <text>  (comment on issue)
        m = re.match(r"echo\s+(\d+)\s+(.+)", cmd, re.IGNORECASE | re.DOTALL)
        if m:
            n = int(m.group(1))
            text = m.group(2).strip()
            return ClassifiedCommand(
                handler="_comment_issue",
                args=(n, text),
                level=CommandLevel.WRITE,
                description=f"Comment on issue #{n}",
            )

        return None

    _ALLOWED_HANDLERS = frozenset({
        "_create_issue", "_list_issues", "_close_issue",
        "_view_issue", "_comment_issue", "_pr_list", "_pr_view",
    })

    async def execute(self, classified: ClassifiedCommand) -> TrackerResult:
        """Execute a previously classified command."""
        handler_name = classified.handler
        if handler_name not in self._ALLOWED_HANDLERS:
            return TrackerResult(
                success=False, output="", error=f"Unknown handler: {handler_name}"
            )
        handler = getattr(self, handler_name)
        result = await handler(*classified.args)
        result.command_level = classified.level
        return result

    # --- Handlers ---

    async def _create_issue(self, title: str, body: str = "") -> TrackerResult:
        args = ["gh", "issue", "create", "--title", title, "--body", body]
        return await self._run(args)

    async def _list_issues(self) -> TrackerResult:
        return await self._run(["gh", "issue", "list", "--state", "open"])

    async def _view_issue(self, number: int) -> TrackerResult:
        return await self._run(["gh", "issue", "view", str(number)])

    async def _close_issue(self, number: int) -> TrackerResult:
        return await self._run(["gh", "issue", "close", str(number)])

    async def _comment_issue(self, number: int, text: str) -> TrackerResult:
        return await self._run(
            ["gh", "issue", "comment", str(number), "--body", text]
        )

    async def _pr_list(self) -> TrackerResult:
        return await self._run(["gh", "pr", "list"])

    async def _pr_view(self, number: int) -> TrackerResult:
        return await self._run(["gh", "pr", "view", str(number)])

    # --- Subprocess runner ---

    async def _run(self, args: list[str]) -> TrackerResult:
        """Execute a gh CLI command via asyncio subprocess."""
        safe_cmd = " ".join(shlex.quote(a) for a in args)
        logger.info("Tracker executing: %s", safe_cmd)

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_dir,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.command_timeout,
            )

            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return TrackerResult(success=True, output=stdout_text or "(no output)")
            else:
                return TrackerResult(
                    success=False,
                    output="",
                    error=stderr_text or f"gh exited with code {proc.returncode}",
                )
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    # Drain pipes, but don't risk hanging indefinitely on a wedged child.
                    await asyncio.wait_for(proc.communicate(), timeout=2)
                except (ProcessLookupError, OSError, asyncio.TimeoutError):
                    pass
            return TrackerResult(
                success=False, output="",
                error=f"Command timed out after {self.command_timeout}s",
            )
        except FileNotFoundError:
            return TrackerResult(
                success=False, output="",
                error="gh CLI not found. Is GitHub CLI installed?",
            )
        except Exception as e:
            return TrackerResult(success=False, output="", error=str(e))
