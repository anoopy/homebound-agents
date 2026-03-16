"""Built-in tracker implementations."""

from homebound.trackers.github import GitHubTracker

TRACKER_REGISTRY: dict[str, type] = {
    "github": GitHubTracker,
}

__all__ = ["GitHubTracker", "TRACKER_REGISTRY"]
