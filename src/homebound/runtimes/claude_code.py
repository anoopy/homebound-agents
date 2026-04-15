"""Claude Code agent runtime.

Implements the AgentRuntime interface for Claude Code interactive CLI.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from homebound.adapters.runtime import AgentRuntime


class ClaudeCodeRuntime(AgentRuntime):
    """Runtime adapter for Claude Code interactive CLI.

    Default configuration uses ``claude`` (safe mode) with ``/exit`` to quit.
    To opt in to auto-accept permissions, set the command explicitly in
    homebound.yaml.
    """

    def __init__(
        self,
        command: str = "claude",
        idle_markers: list[str] | None = None,
        exit_cmd: str = "/exit",
        env_unset: list[str] | None = None,
    ) -> None:
        super().__init__(
            command=command,
            env_unset=env_unset if env_unset is not None else ["CLAUDECODE"],
        )
        self._idle_markers = idle_markers if idle_markers is not None else ["\u276f", "> "]
        self._exit_cmd = exit_cmd

    @classmethod
    def from_config(cls, runtime_config) -> ClaudeCodeRuntime:
        """Create a ClaudeCodeRuntime from RuntimeConfig."""
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

    def start_command(
        self, project_dir: Path, session_id: str = "", session_name: str = "",
    ) -> str:
        extras = ""
        if session_id:
            extras += f" --session-id {session_id}"
        if session_name:
            extras += f" --name {shlex.quote(session_name)}"
        return (
            f"cd {shlex.quote(str(project_dir))} && "
            f"{self._unset_prefix}{self.command}{extras}"
        )

    def resume_command(self, session_id: str) -> str:
        return f"claude --resume {session_id}"
