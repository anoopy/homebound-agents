"""Slack transport implementation.

Implements the Transport ABC for Slack channel communication.
"""

from __future__ import annotations

import logging
import os

import requests

from homebound.adapters.transport import IncomingMessage, Transport

logger = logging.getLogger("homebound.transport.slack")

SLACK_API = "https://slack.com/api"

# Slack Block Kit limit per section text field
_MAX_BLOCK_TEXT = 3000


def _text_to_blocks(text: str) -> list[dict]:
    """Wrap message text in Block Kit section blocks with mrkdwn rendering.

    Slack section blocks have a 3000-char limit per text field.
    Long messages are split at newline boundaries into multiple blocks.
    """
    if not text:
        return []
    if len(text) <= _MAX_BLOCK_TEXT:
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > _MAX_BLOCK_TEXT:
            if current:
                chunks.append(current)
            # If a single line exceeds the limit, hard-truncate it
            current = line[:_MAX_BLOCK_TEXT]
        else:
            current = candidate
    if current:
        chunks.append(current)

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": chunk}}
        for chunk in chunks
    ]


class SlackTransportError(RuntimeError):
    """Raised when Slack API communication fails."""


class SlackTransport(Transport):
    """Slack channel transport for orchestrator communication."""

    def __init__(
        self,
        channel_id: str,
        token: str | None = None,
        token_env: str = "SLACK_BOT_TOKEN",
        message_format: str = "*[{name}]* {message}",
        agent_name: str = "homebound",
        http_timeout: int = 10,
    ) -> None:
        self.channel_id = channel_id
        self.token = token or os.environ.get(token_env, "")
        self.message_format = message_format
        self.agent_name = agent_name
        self.http_timeout = http_timeout
        if not self.token:
            raise ValueError(
                f"{token_env} not set. Add it to your environment or pass token= parameter."
            )
        self._headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    @classmethod
    def from_config(cls, transport_config, agent_name: str) -> SlackTransport:
        """Create a SlackTransport from TransportConfig."""
        return cls(
            channel_id=transport_config.channel_id,
            token_env=transport_config.token_env,
            message_format=transport_config.message_format,
            agent_name=agent_name,
            http_timeout=transport_config.http_timeout,
        )

    def _parse_response(self, resp: requests.Response, context: str) -> dict:
        """Parse and validate a Slack API response.

        Returns the response data dict on success.

        Raises:
            SlackTransportError: On HTTP error, non-JSON response, or Slack API error.
        """
        if not resp.ok:
            raise SlackTransportError(
                f"Slack {context} HTTP error: {resp.status_code}"
            )
        try:
            data = resp.json()
        except ValueError:
            raise SlackTransportError(
                f"Slack {context} returned non-JSON response (status {resp.status_code})"
            )
        if not isinstance(data, dict):
            raise SlackTransportError(
                f"Slack {context} returned unexpected JSON payload type: {type(data).__name__}"
            )
        if not data.get("ok"):
            raise SlackTransportError(
                f"Slack {context} API error: {data.get('error', 'unknown_error')}"
            )
        return data

    def post(self, message: str, thread_ts: str = "") -> str:
        """Post a message to the Slack channel.

        Returns the posted message's Slack timestamp (unique identifier).
        Uses Block Kit section blocks with mrkdwn rendering for rich formatting.
        """
        formatted = self.format_agent_message(self.agent_name, message)
        payload: dict = {
            "channel": self.channel_id,
            "text": formatted,  # fallback for notifications / non-Block Kit clients
            "blocks": _text_to_blocks(formatted),
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            resp = requests.post(
                f"{SLACK_API}/chat.postMessage",
                headers=self._headers,
                json=payload,
                timeout=self.http_timeout,
            )
        except requests.RequestException as e:
            raise SlackTransportError(f"Slack post request failed: {e}") from e
        data = self._parse_response(resp, "post")
        return data.get("ts", "")

    def poll(self, since_ts: float, limit: int = 20) -> list[IncomingMessage]:
        """Fetch recent messages from the Slack channel."""
        oldest = f"{since_ts:.6f}"
        try:
            resp = requests.get(
                f"{SLACK_API}/conversations.history",
                headers=self._headers,
                params={
                    "channel": self.channel_id,
                    "oldest": oldest,
                    "limit": limit,
                },
                timeout=self.http_timeout,
            )
        except requests.RequestException as e:
            raise SlackTransportError(f"Slack poll request failed: {e}") from e
        data = self._parse_response(resp, "poll")
        raw_messages = data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise SlackTransportError(
                f"Slack poll API error: messages payload is {type(raw_messages).__name__}, expected list"
            )

        messages = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                raise SlackTransportError(
                    f"Slack poll API error: message item payload is {type(msg).__name__}, expected object"
                )
            messages.append(
                IncomingMessage(
                    text=msg.get("text", ""),
                    ts=msg.get("ts", ""),
                    user=msg.get("user", ""),
                    thread_ts=msg.get("thread_ts", ""),
                    extra=msg,
                )
            )
        # Slack returns newest-first; reverse to process in chronological order
        messages.reverse()
        return messages

    def poll_thread_replies(
        self,
        thread_parent_ts_list: list[str],
        since_ts: float,
        limit_per_thread: int = 10,
    ) -> list[IncomingMessage]:
        """Fetch replies for active Slack threads via conversations.replies.

        Calls conversations.replies once per parent ts. The parent message
        itself is excluded from results. Per-thread errors are logged as
        warnings and do not abort the remaining threads.

        Returns all replies sorted chronologically (oldest first).
        """
        oldest = f"{since_ts:.6f}"
        all_replies: list[IncomingMessage] = []

        for parent_ts in thread_parent_ts_list:
            try:
                resp = requests.get(
                    f"{SLACK_API}/conversations.replies",
                    headers=self._headers,
                    params={
                        "channel": self.channel_id,
                        "ts": parent_ts,
                        "oldest": oldest,
                        "limit": limit_per_thread,
                    },
                    timeout=self.http_timeout,
                )
                data = self._parse_response(resp, f"poll_thread_replies({parent_ts})")
            except Exception as e:
                logger.warning("Thread reply poll failed for ts=%s: %s", parent_ts, e)
                continue

            raw_messages = data.get("messages", [])
            if not isinstance(raw_messages, list):
                logger.warning(
                    "Thread reply poll for ts=%s returned unexpected messages type: %s",
                    parent_ts, type(raw_messages).__name__,
                )
                continue

            for msg in raw_messages:
                if not isinstance(msg, dict):
                    continue
                msg_ts = msg.get("ts", "")
                # Skip the parent message itself (Slack always includes it first)
                if msg_ts == parent_ts:
                    continue
                all_replies.append(
                    IncomingMessage(
                        text=msg.get("text", ""),
                        ts=msg_ts,
                        user=msg.get("user", ""),
                        thread_ts=msg.get("thread_ts", parent_ts),
                        extra=msg,
                    )
                )

        # Sort all replies chronologically by ts (Slack ts values are sortable as strings)
        all_replies.sort(key=lambda m: m.ts)
        return all_replies

    def format_agent_message(self, name: str, msg: str) -> str:
        """Format a message with the agent's identity prefix."""
        return self.message_format.format(name=name, message=msg)

    def is_from_agent(self, msg: IncomingMessage, prefixes: list[str]) -> bool:
        """Check if a message was sent by a known agent."""
        return any(f"[{prefix}" in msg.text for prefix in prefixes)
