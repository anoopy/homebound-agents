"""Abstract base class for agent runtimes.

An AgentRuntime encapsulates the CLI-specific details of starting,
detecting idle state, and exiting an interactive AI agent process.
"""

from __future__ import annotations

import re
import shlex
from abc import ABC, abstractmethod
from pathlib import Path

_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_env_names(names: list[str]) -> None:
    """Validate that all names are safe shell variable identifiers."""
    for name in names:
        if not _ENV_VAR_RE.match(name):
            raise ValueError(
                f"Invalid environment variable name in env_unset: {name!r}. "
                f"Must match [A-Za-z_][A-Za-z0-9_]*"
            )


class AgentRuntime(ABC):
    """Abstract interface for CLI tools managed as tmux child sessions.

    Subclasses must implement idle_prompt_markers() and exit_command().
    The start_command() and env_overrides() methods have default
    implementations based on self.command and self._env_unset.
    """

    def __init__(
        self,
        command: str,
        env_unset: list[str] | None = None,
    ) -> None:
        self.command = command
        self._env_unset = env_unset if env_unset is not None else []
        _validate_env_names(self._env_unset)
        # Pre-compute derived state since _env_unset is immutable after init
        unset_parts = " && ".join(f"unset {v}" for v in self._env_unset)
        self._unset_prefix = f"{unset_parts} && " if unset_parts else ""
        self._env_overrides: dict[str, str | None] = {v: None for v in self._env_unset}

    def start_command(
        self, project_dir: Path, session_id: str = "", session_name: str = "",
    ) -> str:
        """Return the shell command to start the CLI in a tmux window.

        Args:
            project_dir: Absolute path to the project working directory.
            session_id: Optional pre-assigned session UUID (used by runtimes
                that support it, ignored otherwise).
            session_name: Optional human-readable session label (used by
                runtimes that support it, ignored otherwise).

        Returns:
            A shell command string (passed to tmux send-keys).
        """
        return f"cd {shlex.quote(str(project_dir))} && {self._unset_prefix}{self.command}"

    def supports_session_resume(self) -> bool:
        """Whether this runtime supports session resumption."""
        return False

    def resume_command(self, session_id: str) -> str:
        """Return the CLI command a user would run to resume this session.

        Returns an empty string if the runtime doesn't support resume.
        """
        return ""

    @abstractmethod
    def idle_prompt_markers(self) -> list[str]:
        """Return strings that indicate the CLI is idle at its prompt.

        These are checked against the last few lines of tmux capture-pane
        output to detect when the CLI is ready to accept input.

        Returns:
            List of marker strings (any match = idle).
        """

    @abstractmethod
    def exit_command(self) -> str:
        """Return the command to gracefully exit the CLI.

        Returns:
            A string to send via tmux send-keys (e.g. "/exit", "exit").
        """

    def env_overrides(self) -> dict[str, str | None]:
        """Return environment variable overrides for the child process.

        Keys mapped to None will be unset. Keys mapped to strings will
        be set to that value.

        Returns:
            Dict of env var name -> value (or None to unset).
        """
        return dict(self._env_overrides)
