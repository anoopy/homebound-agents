"""Abstract base class for issue/work-item trackers.

A Tracker proxies structured commands to an external issue tracker
(GitHub, Linear, Jira, etc.) and classifies them by security level.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class CommandLevel(Enum):
    """Security classification for tracker commands."""

    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


@dataclass
class ClassifiedCommand:
    """Result of classifying a tracker command."""

    handler: str
    args: tuple
    level: CommandLevel
    description: str


@dataclass
class TrackerResult:
    """Result of a tracker command dispatch."""

    success: bool
    output: str
    error: str = ""
    command_level: CommandLevel = CommandLevel.READ


class Tracker(ABC):
    """Abstract interface for issue/work-item tracker integration."""

    @abstractmethod
    def classify(self, command_text: str) -> ClassifiedCommand | None:
        """Classify a command without executing it.

        Returns:
            ClassifiedCommand if a pattern matched, None otherwise.
        """

    @abstractmethod
    async def execute(self, classified: ClassifiedCommand) -> TrackerResult:
        """Execute a previously classified command."""

    async def dispatch(self, command_text: str) -> TrackerResult | None:
        """Classify and execute a command in one call.

        Returns:
            TrackerResult on match, None if no pattern matched.
        """
        classified = self.classify(command_text)
        if classified is None:
            return None
        return await self.execute(classified)
