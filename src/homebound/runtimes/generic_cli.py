"""Generic CLI agent runtime.

Implements the AgentRuntime interface for arbitrary CLI tools/REPLs.
"""

from __future__ import annotations

from homebound.adapters.runtime import AgentRuntime


class GenericCLIRuntime(AgentRuntime):
    """Runtime adapter for arbitrary CLI tools (shells, REPLs, etc.).

    Provides sensible defaults for generic shell-like CLIs while allowing
    full customization via constructor arguments.
    """

    def __init__(
        self,
        command: str,
        prompt_markers: list[str] | None = None,
        exit_cmd: str = "exit",
        env_unset: list[str] | None = None,
    ) -> None:
        super().__init__(command=command, env_unset=env_unset)
        self._prompt_markers = prompt_markers if prompt_markers is not None else ["$ ", "> "]
        self._exit_cmd = exit_cmd

    @classmethod
    def from_config(cls, runtime_config) -> GenericCLIRuntime:
        """Create a GenericCLIRuntime from RuntimeConfig."""
        return cls(
            command=runtime_config.command,
            prompt_markers=runtime_config.idle_markers,
            exit_cmd=runtime_config.exit_command,
            env_unset=runtime_config.env_unset,
        )

    def idle_prompt_markers(self) -> list[str]:
        return list(self._prompt_markers)

    def exit_command(self) -> str:
        return self._exit_cmd
