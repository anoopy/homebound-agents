"""Built-in transport implementations."""

from homebound.transports.slack import SlackTransport

TRANSPORT_REGISTRY: dict[str, type] = {
    "slack": SlackTransport,
}

__all__ = ["SlackTransport", "TRANSPORT_REGISTRY"]
