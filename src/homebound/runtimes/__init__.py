"""Built-in agent runtime implementations."""

from homebound.runtimes.claude_code import ClaudeCodeRuntime
from homebound.runtimes.generic_cli import GenericCLIRuntime

RUNTIME_REGISTRY: dict[str, type] = {
    "claude-code": ClaudeCodeRuntime,
    "generic": GenericCLIRuntime,
}

__all__ = ["ClaudeCodeRuntime", "GenericCLIRuntime", "RUNTIME_REGISTRY"]
