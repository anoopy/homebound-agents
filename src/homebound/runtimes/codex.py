"""Codex CLI agent runtime.

Implements the AgentRuntime interface for OpenAI Codex interactive CLI,
with session ID discovery for resume support.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from homebound.adapters.runtime import AgentRuntime
from homebound.tmux import run_tmux

logger = logging.getLogger("homebound.codex_runtime")


class CodexRuntime(AgentRuntime):
    """Runtime adapter for OpenAI Codex interactive CLI.

    Supports session resumption via post-launch discovery of session UUIDs
    from ``~/.codex/sessions/``.
    """

    def __init__(
        self,
        command: str = "codex",
        idle_markers: list[str] | None = None,
        exit_cmd: str = "/exit",
        env_unset: list[str] | None = None,
    ) -> None:
        super().__init__(command=command, env_unset=env_unset or [])
        self._idle_markers = idle_markers if idle_markers is not None else ["\u203a"]
        self._exit_cmd = exit_cmd

    @classmethod
    def from_config(cls, runtime_config) -> CodexRuntime:
        """Create a CodexRuntime from RuntimeConfig."""
        return cls(
            command=runtime_config.command,
            idle_markers=runtime_config.idle_markers,
            exit_cmd=runtime_config.exit_command,
            env_unset=runtime_config.env_unset,
        )

    def idle_prompt_markers(self) -> list[str]:
        return list(self._idle_markers)

    def exit_command(self) -> str:
        return self._exit_cmd

    def supports_session_resume(self) -> bool:
        return True

    def resume_command(self, session_id: str) -> str:
        return f"codex resume {session_id}"

    async def discover_session_id(
        self, tmux_session: str, window_name: str,
    ) -> str:
        """Discover the Codex session UUID from ~/.codex/sessions/.

        Strategy:
        1. Get the tmux pane PID (the shell process).
        2. Find the codex child process via pgrep.
        3. Scan today's session directory for the most recent rollout file.
        4. Parse the first JSONL line (session_meta) to extract the session ID.

        Returns the session UUID string, or "" if not found.
        """
        target = f"{tmux_session}:{window_name}"

        # Step 1: get pane PID
        rc, pane_pid_raw, _ = await run_tmux(
            "display-message", "-p", "-t", target, "#{pane_pid}",
        )
        if rc != 0 or not pane_pid_raw.strip():
            logger.debug("Could not get pane PID for %s", target)
            return ""

        pane_pid = pane_pid_raw.strip()

        # Step 2: find codex child process(es)
        try:
            proc = await asyncio.create_subprocess_exec(
                "pgrep", "-P", pane_pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            child_pids = [p.strip() for p in stdout.decode().strip().splitlines() if p.strip()]
        except Exception:
            logger.debug("pgrep failed for pane PID %s", pane_pid)
            return ""

        if not child_pids:
            logger.debug("No child processes found for pane PID %s", pane_pid)
            return ""

        # Step 3: scan ~/.codex/sessions/ for most recent session file
        sessions_base = Path.home() / ".codex" / "sessions"
        if not sessions_base.is_dir():
            logger.debug("Codex sessions directory not found: %s", sessions_base)
            return ""

        # Find the most recent rollout file across date directories
        rollout_files: list[Path] = []
        try:
            for year_dir in sorted(sessions_base.iterdir(), reverse=True):
                if not year_dir.is_dir():
                    continue
                for month_dir in sorted(year_dir.iterdir(), reverse=True):
                    if not month_dir.is_dir():
                        continue
                    for day_dir in sorted(month_dir.iterdir(), reverse=True):
                        if not day_dir.is_dir():
                            continue
                        for f in sorted(day_dir.iterdir(), reverse=True):
                            if f.name.startswith("rollout-") and f.suffix == ".jsonl":
                                rollout_files.append(f)
                        if rollout_files:
                            break  # Only check most recent day with files
                    if rollout_files:
                        break
                if rollout_files:
                    break
        except Exception as e:
            logger.debug("Error scanning codex sessions: %s", e)
            return ""

        # Step 4: check each recent file for a matching PID
        for rollout_file in rollout_files[:10]:  # Check up to 10 most recent
            try:
                with open(rollout_file) as f:
                    first_line = f.readline()
                if not first_line:
                    continue
                entry = json.loads(first_line)
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload", {})
                session_id = payload.get("id", "")
                if session_id:
                    # Match: return the most recent session from today
                    # (Codex doesn't embed PID in session_meta, so we use
                    # recency as the primary matching heuristic)
                    return session_id
            except Exception:
                continue

        logger.debug("No codex session ID found for pane PID %s", pane_pid)
        return ""
