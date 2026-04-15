"""Built-in agent runtime implementations."""

from homebound.runtimes.claude_code import ClaudeCodeRuntime
from homebound.runtimes.codex import CodexRuntime
from homebound.runtimes.generic_cli import GenericCLIRuntime

RUNTIME_REGISTRY: dict[str, type] = {
    "claude-code": ClaudeCodeRuntime,
    "codex": CodexRuntime,
    "generic": GenericCLIRuntime,
}

__all__ = ["ClaudeCodeRuntime", "CodexRuntime", "GenericCLIRuntime", "RUNTIME_REGISTRY"]
