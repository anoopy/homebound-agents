"""Tests for centralized command authorization policy."""

from __future__ import annotations

from homebound.config import SecurityConfig
from homebound.security import CommandAction, CommandPolicy, Principal


class TestCommandPolicy:
    def test_intake_denied_by_secure_default(self):
        policy = CommandPolicy(SecurityConfig())
        decision = policy.evaluate(CommandAction.INTAKE, Principal(user_id="WUSER"))
        assert not decision.allow

    def test_intake_allowed_for_open_channel_authenticated_human(self):
        policy = CommandPolicy(SecurityConfig(allow_open_channel=True))
        decision = policy.evaluate(CommandAction.INTAKE, Principal(user_id="WUSER"))
        assert decision.allow

    def test_secure_default_denies_without_allowlist(self):
        policy = CommandPolicy(SecurityConfig())
        decision = policy.evaluate(
            CommandAction.SESSION_SPAWN,
            Principal(user_id="WUSER"),
        )
        assert not decision.allow

    def test_open_channel_allows_authenticated_human_for_session_actions(self):
        policy = CommandPolicy(SecurityConfig(allow_open_channel=True))
        decision = policy.evaluate(
            CommandAction.SESSION_SPAWN,
            Principal(user_id="WUSER"),
        )
        assert decision.allow

    def test_tracker_write_requires_allowlisted_user(self):
        policy = CommandPolicy(SecurityConfig(allow_open_channel=True))
        decision = policy.evaluate(
            CommandAction.TRACKER_WRITE,
            Principal(user_id="WUSER"),
        )
        assert not decision.allow

    def test_allowlisted_user_can_write_tracker(self):
        policy = CommandPolicy(SecurityConfig(allowed_users=["WADMIN"]))
        decision = policy.evaluate(
            CommandAction.TRACKER_WRITE,
            Principal(user_id="WADMIN"),
        )
        assert decision.allow

    def test_owner_enforced_for_session_route(self):
        policy = CommandPolicy(SecurityConfig(allowed_users=["WOWNER", "WADMIN"]))
        owner_ok = policy.evaluate(
            CommandAction.SESSION_ROUTE,
            Principal(user_id="WOWNER"),
            owner_user_id="WOWNER",
        )
        rando_denied = policy.evaluate(
            CommandAction.SESSION_ROUTE,
            Principal(user_id="WRANDO"),
            owner_user_id="WOWNER",
        )
        assert owner_ok.allow
        assert not rando_denied.allow

    def test_admin_takeover_optional(self):
        policy = CommandPolicy(
            SecurityConfig(
                allowed_users=["WOWNER", "WADMIN"],
                allow_admin_takeover=True,
            )
        )
        decision = policy.evaluate(
            CommandAction.SESSION_CLOSE,
            Principal(user_id="WADMIN"),
            owner_user_id="WOWNER",
        )
        assert decision.allow

    def test_bot_denied_by_default(self):
        policy = CommandPolicy(SecurityConfig(allow_open_channel=True))
        decision = policy.evaluate(
            CommandAction.SESSION_SPAWN,
            Principal(user_id="", is_bot=True),
        )
        assert not decision.allow

    def test_bot_can_be_allowed_in_explicit_open_channel_mode(self):
        policy = CommandPolicy(
            SecurityConfig(allow_open_channel=True, allow_bots=True),
        )
        decision = policy.evaluate(
            CommandAction.SESSION_SPAWN,
            Principal(user_id="WBOT", is_bot=True),
        )
        assert decision.allow

    def test_allowlisted_bot_can_operate_without_open_channel(self):
        policy = CommandPolicy(
            SecurityConfig(
                allowed_users=["WBOT"],
                allow_bots=True,
                allow_open_channel=False,
            ),
        )
        decision = policy.evaluate(
            CommandAction.SESSION_SPAWN,
            Principal(user_id="WBOT", is_bot=True),
        )
        assert decision.allow

    def test_non_allowlisted_bot_denied_when_allowlist_active(self):
        policy = CommandPolicy(
            SecurityConfig(
                allowed_users=["WADMIN"],
                allow_bots=True,
                allow_open_channel=False,
            ),
        )
        decision = policy.evaluate(
            CommandAction.SESSION_SPAWN,
            Principal(user_id="WBOT", is_bot=True),
        )
        assert not decision.allow

    def test_prompt_answer_requires_allowlisted_sender_when_allowlist_present(self):
        policy = CommandPolicy(SecurityConfig(allowed_users=["WALLOWED"]))
        allowed = policy.evaluate(
            CommandAction.PROMPT_ANSWER,
            Principal(user_id="WALLOWED"),
        )
        denied = policy.evaluate(
            CommandAction.PROMPT_ANSWER,
            Principal(user_id="WDENIED"),
        )
        assert allowed.allow
        assert not denied.allow

    def test_prompt_answer_open_channel_fallback_requires_authenticated_sender(self):
        policy = CommandPolicy(SecurityConfig(allow_open_channel=True))
        allowed = policy.evaluate(
            CommandAction.PROMPT_ANSWER,
            Principal(user_id="WUSER"),
        )
        denied = policy.evaluate(
            CommandAction.PROMPT_ANSWER,
            Principal(user_id=""),
        )
        assert allowed.allow
        assert not denied.allow
