"""Abstract base class for message transports.

A Transport handles communication between the orchestrator and the
outside world (Slack, Discord, Telegram, etc.).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class IncomingMessage:
    """A message received from the transport layer."""

    text: str
    ts: str  # Unique message identifier / timestamp
    user: str = ""  # User ID who sent the message
    thread_ts: str = ""  # Parent thread ts (empty = top-level message)
    extra: dict = field(default_factory=dict)  # Transport-specific metadata


class Transport(ABC):
    """Abstract interface for message transport layers."""

    @abstractmethod
    def post(self, message: str, thread_ts: str = "") -> str:
        """Post a message to the configured channel.

        Args:
            message: The message text to send.
            thread_ts: If non-empty, post as a reply in this thread.

        Returns:
            The posted message's unique timestamp/identifier.
        """

    @abstractmethod
    def poll(self, since_ts: float, limit: int = 20) -> list[IncomingMessage]:
        """Fetch recent messages from the channel.

        Args:
            since_ts: Unix timestamp — only return messages after this time.
            limit: Maximum number of messages to return.

        Returns:
            List of IncomingMessage objects, oldest first.
        """

    def poll_thread_replies(
        self,
        thread_parent_ts_list: list[str],
        since_ts: float,
        limit_per_thread: int = 10,
    ) -> list[IncomingMessage]:
        """Fetch replies for active threads.

        Default no-op — subclasses may override for transports that require
        explicit per-thread polling (e.g. Slack conversations.replies).

        Args:
            thread_parent_ts_list: Parent message timestamps to poll.
            since_ts: Unix timestamp — only return replies after this time.
            limit_per_thread: Max replies to fetch per thread.

        Returns:
            List of IncomingMessage objects, oldest first across all threads.
        """
        return []

    @abstractmethod
    def format_agent_message(self, name: str, msg: str) -> str:
        """Format a message with the agent's identity prefix.

        Args:
            name: The agent/orchestrator name.
            msg: The message body.

        Returns:
            Formatted message string.
        """

    @abstractmethod
    def is_from_agent(self, msg: IncomingMessage, prefixes: list[str]) -> bool:
        """Check if a message was sent by a known agent (to prevent loops).

        Args:
            msg: The incoming message to check.
            prefixes: List of agent name prefixes to match against.

        Returns:
            True if the message appears to be from an agent.
        """
