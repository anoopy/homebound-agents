"""Centralized command authorization policy for Homebound."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from homebound.config import SecurityConfig


class CommandAction(Enum):
    """Actions requiring policy decisions."""

    INTAKE = "intake"
    SESSION_SPAWN = "session.spawn"
    SESSION_ROUTE = "session.route"
    SESSION_CLOSE = "session.close"
    TRACKER_READ = "tracker.read"
    TRACKER_WRITE = "tracker.write"
    TRACKER_DESTRUCTIVE = "tracker.destructive"
    ADMIN_STATUS = "admin.status"
    ADMIN_HELP = "admin.help"
    ADMIN_ISSUE_STATUS = "admin.issue_status"
    ADMIN_SKILLS = "admin.skills"
    PROMPT_ANSWER = "prompt.answer"


@dataclass(frozen=True)
class Principal:
    """Sender identity information derived from transport metadata."""

    user_id: str
    is_bot: bool = False

    @property
    def is_authenticated(self) -> bool:
        return bool(self.user_id)


@dataclass(frozen=True)
class Decision:
    """Authorization decision output."""

    allow: bool
    reason: str = ""


class CommandPolicy:
    """Single source of truth for inbound command authorization."""

    def __init__(self, security: SecurityConfig) -> None:
        self.security = security

    def _is_allowlisted(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self.security.allowed_users

    def evaluate(
        self,
        action: CommandAction,
        principal: Principal,
        owner_user_id: str = "",
    ) -> Decision:
        """Evaluate whether a principal can execute an action."""
        has_allowlist = bool(self.security.allowed_users)
        is_allowlisted = self._is_allowlisted(principal.user_id)

        if principal.is_bot:
            if not self.security.allow_bots:
                return Decision(False, "bot or webhook senders are denied by policy")
            if has_allowlist:
                if not is_allowlisted:
                    return Decision(False, "bot or webhook sender is not allowlisted")
            elif not self.security.allow_open_channel:
                return Decision(False, "bot or webhook senders require allow_open_channel=true")
        else:
            if not principal.is_authenticated:
                return Decision(False, "authenticated user required")

            if has_allowlist:
                if not is_allowlisted:
                    return Decision(False, "sender is not allowlisted")
            elif not self.security.allow_open_channel:
                return Decision(
                    False,
                    "allowlist is empty and open-channel mode is disabled",
                )

        if action in (CommandAction.TRACKER_WRITE, CommandAction.TRACKER_DESTRUCTIVE):
            if not is_allowlisted:
                return Decision(False, "tracker write/destructive actions require allowlisted sender")

        if action in (CommandAction.SESSION_ROUTE, CommandAction.SESSION_CLOSE):
            if owner_user_id and principal.user_id != owner_user_id:
                if not (self.security.allow_admin_takeover and is_allowlisted):
                    return Decision(False, "only the session owner can control this session")

        return Decision(True)
