"""Tests for the Orchestrator class."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homebound.config import HomeboundConfig, SecurityConfig
from homebound.session import ChildInfo, list_custom_skills


class TestChatMode:
    """Verify chat mode spawns children with a simpler prompt."""

    @pytest.fixture
    def mock_tmux(self):
        with (
            patch("homebound.session.run_tmux", new_callable=AsyncMock) as mock_run,
            patch("homebound.session.wait_for_prompt", new_callable=AsyncMock) as mock_wait,
            patch("homebound.session.send_keys", new_callable=AsyncMock) as mock_send,
            patch("homebound.session.new_window", new_callable=AsyncMock) as mock_new_win,
            patch("homebound.session.kill_window", new_callable=AsyncMock) as mock_kill_win,
        ):
            mock_run.return_value = (0, "", "")
            mock_wait.return_value = True
            mock_send.return_value = True
            yield {
                "run_tmux": mock_run,
                "wait_for_prompt": mock_wait,
                "send_keys": mock_send,
                "new_window": mock_new_win,
                "kill_window": mock_kill_win,
            }

    def test_chat_mode_sends_simple_prompt(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        child = asyncio.run(spawn_child(42, "check the test output", config, mode="chat"))

        mock_send = mock_tmux["send_keys"]
        mock_send.assert_called_once()
        _, sent_prompt = mock_send.call_args.args
        assert "Agent42" in sent_prompt
        assert "check the test output" in sent_prompt
        assert "COMMUNICATION RULES" not in sent_prompt
        assert "gh issue view" not in sent_prompt

    def test_task_mode_sends_structured_prompt(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        child = asyncio.run(spawn_child(215, "implement feature", config, mode="task"))

        mock_send = mock_tmux["send_keys"]
        mock_send.assert_called_once()
        _, sent_prompt = mock_send.call_args.args
        assert "Work on item Agent215" in sent_prompt

    def test_default_mode_is_task(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        asyncio.run(spawn_child(100, "some task", config))

        mock_send = mock_tmux["send_keys"]
        _, sent_prompt = mock_send.call_args.args
        assert "Work on item Agent100" in sent_prompt

    def test_freeform_mode_sends_legacy_prompt(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        asyncio.run(spawn_child(215, "check test output", config, mode="freeform"))

        mock_send = mock_tmux["send_keys"]
        _, sent_prompt = mock_send.call_args.args
        assert "Agent215" in sent_prompt
        assert "BEGIN TASK" in sent_prompt
        assert "check test output" in sent_prompt
        assert "COMMUNICATION RULES" in sent_prompt
        assert "Work on item Agent" not in sent_prompt

    def test_session_identity_uses_dev_prefix_for_issue_sessions(self, mock_tmux):
        from homebound.session import session_name

        config = HomeboundConfig()
        assert session_name(config, 240) == "agent-240"

    def test_window_name_uses_dev_prefix(self, mock_tmux):
        from homebound.session import window_name

        config = HomeboundConfig()
        assert window_name(config, 3) == "AGENT-3"

    def test_spawn_fails_when_start_command_fails(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        mock_tmux["run_tmux"].return_value = (1, "", "cannot start")

        with pytest.raises(RuntimeError, match="Failed to start CLI"):
            asyncio.run(spawn_child(42, "check output", config))

        mock_tmux["kill_window"].assert_awaited_once()

    def test_spawn_fails_when_prompt_not_ready(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        mock_tmux["wait_for_prompt"].return_value = False

        with pytest.raises(RuntimeError, match="Timed out waiting for CLI prompt"):
            asyncio.run(spawn_child(42, "check output", config))

        mock_tmux["kill_window"].assert_awaited_once()

    def test_spawn_fails_when_initial_prompt_send_fails(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        mock_tmux["send_keys"].return_value = False

        with pytest.raises(RuntimeError, match="Failed to send initial prompt"):
            asyncio.run(spawn_child(42, "check output", config))

        mock_tmux["kill_window"].assert_awaited_once()


class TestAdminQueries:
    """Verify admin command parsing and routing."""

    @pytest.fixture
    def orchestrator(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
            return orch

    def test_status_command(self, orchestrator):
        orchestrator._admin.report_sessions = AsyncMock()
        asyncio.run(orchestrator._admin.handle_admin_query("status", sender_user_id="WUSER"))
        orchestrator._admin.report_sessions.assert_awaited_once()

    def test_help_command(self, orchestrator):
        asyncio.run(orchestrator._admin.handle_admin_query("help", sender_user_id="WUSER"))
        orchestrator._post.assert_awaited_once()
        msg = orchestrator._post.call_args.args[0]
        assert "help" in msg.lower() or "admin" in msg.lower() or "status" in msg.lower()
        assert "skills" in msg.lower()

    def test_help_command_shows_aliases(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        config.orchestrator.aliases = ["hb"]
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
        asyncio.run(orch._admin.handle_admin_query("help", sender_user_id="WUSER"))
        msg = orch._post.call_args.args[0]
        assert "@hb" in msg

    def test_help_command_no_alias_line_when_empty(self, orchestrator):
        asyncio.run(orchestrator._admin.handle_admin_query("help", sender_user_id="WUSER"))
        msg = orchestrator._post.call_args.args[0]
        assert "Aliases" not in msg

    def test_issue_query(self, orchestrator):
        orchestrator._admin.report_issue_status = AsyncMock()
        asyncio.run(orchestrator._admin.handle_admin_query("42?", sender_user_id="WUSER"))
        orchestrator._admin.report_issue_status.assert_awaited_once_with(42)

    def test_unknown_command(self, orchestrator):
        from homebound.trackers.github import GitHubTracker
        mock_tracker = MagicMock(spec=GitHubTracker)
        mock_tracker.classify.return_value = None
        orchestrator._admin._tracker_fn = lambda: mock_tracker

        asyncio.run(orchestrator._admin.handle_admin_query("xyzzy", sender_user_id="WUSER"))
        orchestrator._post.assert_awaited_once()
        msg = orchestrator._post.call_args.args[0]
        assert "unknown" in msg.lower() or "help" in msg.lower()

    def test_natural_language_open_query_maps_to_status(self, orchestrator):
        orchestrator._admin.report_sessions = AsyncMock()
        asyncio.run(
            orchestrator._admin.handle_admin_query("what is open right now?", sender_user_id="WUSER"),
        )
        orchestrator._admin.report_sessions.assert_awaited_once()

    def test_natural_language_sessions_query_maps_to_status(self, orchestrator):
        orchestrator._admin.report_sessions = AsyncMock()

        asyncio.run(
            orchestrator._admin.handle_admin_query(
                "which sessions are open right now",
                sender_user_id="WUSER",
            ),
        )

        orchestrator._admin.report_sessions.assert_awaited_once()

    def test_natural_language_sessions_query_with_punctuation_maps_to_status(self, orchestrator):
        orchestrator._admin.report_sessions = AsyncMock()

        asyncio.run(
            orchestrator._admin.handle_admin_query(
                "which sessions are open right now;",
                sender_user_id="WUSER",
            ),
        )

        orchestrator._admin.report_sessions.assert_awaited_once()


class TestSecurityHardening:
    """Verify allowlist and session authorization."""

    def test_secure_default_blocks_when_allowlist_empty(self):
        config = HomeboundConfig()
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            assert orch._is_user_denied("U123") is True

    def test_allowlist_blocks_unauthorized(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allowed_users=["WFAKE_ALICE"])
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            assert orch._is_user_denied("WFAKE_NOBODY") is True

    def test_allowlist_allows_authorized(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allowed_users=["WFAKE_ALICE"])
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            assert orch._is_user_denied("WFAKE_ALICE") is False

    def test_bot_messages_denied_when_allowlist_active(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allowed_users=["WFAKE_ALICE"])
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            # Bot/webhook messages (empty user_id) are denied when allowlist is active
            assert orch._is_user_denied("") is True

    def test_open_channel_allows_authenticated_humans(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            assert orch._is_user_denied("WFAKE_USER") is False

    def test_bot_messages_denied_by_default_even_open_channel(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            assert orch._is_user_denied("", msg_extra={"bot_id": "B123"}) is True

    def test_allowlisted_bot_id_allowed_when_bots_enabled(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(
            allowed_users=["B123"], allow_bots=True, allow_open_channel=False
        )
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            assert orch._is_user_denied("", msg_extra={"bot_id": "B123"}) is False

    def test_bot_identity_prefers_bot_id_when_user_also_present(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(
            allowed_users=["B123"], allow_bots=True, allow_open_channel=False
        )
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            assert orch._is_user_denied(
                "WFAKE_USER",
                msg_extra={"subtype": "bot_message", "bot_id": "B123"},
            ) is False

    def test_session_owner_can_follow_up(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allowed_users=["WFAKE_ADMIN", "WFAKE_OWNER"])
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WFAKE_OWNER")
            assert orch._is_session_authorized("WFAKE_OWNER", child) is True

    def test_session_non_owner_blocked(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allowed_users=["WFAKE_ADMIN"])
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WFAKE_OWNER")
            assert orch._is_session_authorized("WFAKE_RANDO", child) is False

    def test_session_admin_override(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(
            allowed_users=["WFAKE_ADMIN"], allow_admin_takeover=True
        )
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WFAKE_OWNER")
            assert orch._is_session_authorized("WFAKE_ADMIN", child) is True

    def test_unowned_session_requires_authenticated_sender(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="")
            assert orch._is_session_authorized("WFAKE_RANDO", child) is True


class TestDestructiveConfirmation:
    """Verify destructive command confirmation flow."""

    @pytest.fixture
    def orchestrator(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allowed_users=["WFAKE_USER1"])
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
            return orch

    def test_destructive_requires_confirmation(self, orchestrator):
        from homebound.adapters.tracker import ClassifiedCommand, CommandLevel
        from homebound.trackers.github import GitHubTracker

        mock_tracker = MagicMock(spec=GitHubTracker)
        mock_tracker.classify.return_value = ClassifiedCommand(
            handler="_close_issue", args=(42,),
            level=CommandLevel.DESTRUCTIVE,
            description="Close issue #42",
        )
        mock_tracker.execute = AsyncMock()
        orchestrator._admin._tracker_fn = lambda: mock_tracker

        asyncio.run(orchestrator._admin.handle_admin_query("rm 42", sender_user_id="WFAKE_USER1"))

        mock_tracker.execute.assert_not_awaited()
        msg = orchestrator._post.call_args.args[0]
        assert "confirm" in msg.lower() or "repeat" in msg.lower()

    def test_destructive_confirmed_executes(self, orchestrator):
        from homebound.adapters.tracker import ClassifiedCommand, CommandLevel, TrackerResult
        from homebound.trackers.github import GitHubTracker

        mock_tracker = MagicMock(spec=GitHubTracker)
        mock_tracker.classify.return_value = ClassifiedCommand(
            handler="_close_issue", args=(42,),
            level=CommandLevel.DESTRUCTIVE,
            description="Close issue #42",
        )
        mock_tracker.execute = AsyncMock(
            return_value=TrackerResult(success=True, output="Closed issue #42")
        )
        orchestrator._admin._tracker_fn = lambda: mock_tracker
        # Confirm key stores normalized command text.
        orchestrator._admin._pending_confirms[("WFAKE_USER1", "rm 42")] = time.time()

        asyncio.run(orchestrator._admin.handle_admin_query("rm 42", sender_user_id="WFAKE_USER1"))

        mock_tracker.execute.assert_awaited_once()
        msg = orchestrator._post.call_args.args[0]
        assert "Closed issue #42" in msg

    def test_confirmation_expires(self, orchestrator):
        from homebound.adapters.tracker import ClassifiedCommand, CommandLevel
        from homebound.trackers.github import GitHubTracker

        mock_tracker = MagicMock(spec=GitHubTracker)
        mock_tracker.classify.return_value = ClassifiedCommand(
            handler="_close_issue", args=(42,),
            level=CommandLevel.DESTRUCTIVE,
            description="Close issue #42",
        )
        mock_tracker.execute = AsyncMock()
        orchestrator._admin._tracker_fn = lambda: mock_tracker
        # Use config timeout + 1 to ensure expiry regardless of default
        timeout = orchestrator.config.security.destructive_confirm_timeout
        orchestrator._admin._pending_confirms[("WFAKE_USER1", "rm 42")] = time.time() - (timeout + 1)

        asyncio.run(orchestrator._admin.handle_admin_query("rm 42", sender_user_id="WFAKE_USER1"))

        mock_tracker.execute.assert_not_awaited()

    def test_destructive_blocked_for_empty_sender(self, orchestrator):
        from homebound.adapters.tracker import ClassifiedCommand, CommandLevel
        from homebound.trackers.github import GitHubTracker

        mock_tracker = MagicMock(spec=GitHubTracker)
        mock_tracker.classify.return_value = ClassifiedCommand(
            handler="_close_issue", args=(42,),
            level=CommandLevel.DESTRUCTIVE,
            description="Close issue #42",
        )
        mock_tracker.execute = AsyncMock()
        orchestrator._admin._tracker_fn = lambda: mock_tracker

        asyncio.run(orchestrator._admin.handle_admin_query("rm 42", sender_user_id=""))

        mock_tracker.execute.assert_not_awaited()
        orchestrator._post.assert_not_awaited()

    def test_destructive_confirmation_uses_normalized_text(self, orchestrator):
        from homebound.adapters.tracker import ClassifiedCommand, CommandLevel, TrackerResult
        from homebound.trackers.github import GitHubTracker

        mock_tracker = MagicMock(spec=GitHubTracker)
        mock_tracker.classify.return_value = ClassifiedCommand(
            handler="_close_issue", args=(42,),
            level=CommandLevel.DESTRUCTIVE,
            description="Close issue #42",
        )
        mock_tracker.execute = AsyncMock(
            return_value=TrackerResult(success=True, output="Closed issue #42")
        )
        orchestrator._admin._tracker_fn = lambda: mock_tracker
        orchestrator._admin._pending_confirms[("WFAKE_USER1", "rm 42")] = time.time()

        asyncio.run(orchestrator._admin.handle_admin_query("  RM   42  ", sender_user_id="WFAKE_USER1"))

        mock_tracker.execute.assert_awaited_once()


class TestPollCycleReliability:
    """Verify poll-cycle behavior under transport failures."""

    @pytest.fixture
    def orchestrator(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
            return orch

    def test_health_check_runs_when_poll_fails(self, orchestrator):
        orchestrator._retry_transport = AsyncMock(side_effect=RuntimeError("poll failed"))
        orchestrator._health_check = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._health_check.assert_awaited_once()

    def test_poll_cycle_preserves_bot_id_as_sender_identity(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator.config.security = SecurityConfig(
            allowed_users=["B123"], allow_bots=True
        )
        orchestrator.command_policy = orchestrator.command_policy.__class__(orchestrator.config.security)
        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent implement feature",
                    ts=str(time.time() + 1),
                    user="",
                    extra={"bot_id": "B123"},
                )
            ]
        )
        orchestrator._health_check = AsyncMock()
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_awaited_once()
        assert orchestrator._handle_issue_message.call_args.kwargs["sender_user_id"] == "B123"


class TestPromptRelay:
    """Verify runtime prompt relay detection and answer flow."""

    @pytest.fixture
    def orchestrator(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
            return orch

    def test_detect_prompt_from_output(self, orchestrator):
        detected = orchestrator._prompt_relay.detect_prompt_from_output(
            "Select deployment target?\n1. staging\n2. production\n"
        )
        assert detected is not None
        question, options = detected
        assert question == "Select deployment target?"
        assert options == ["staging", "production"]

    def test_detect_prompt_ignores_noise(self, orchestrator):
        detected = orchestrator._prompt_relay.detect_prompt_from_output(
            "build started\ncompleted in 4.2s\nall checks passed\n"
        )
        assert detected is None

    def test_detect_prompt_uses_latest_option_block(self, orchestrator):
        detected = orchestrator._prompt_relay.detect_prompt_from_output(
            "Old prompt?\n1. old-a\n2. old-b\n"
            "info: still running\n"
            "New prompt?\n1. new-a\n2. new-b\n"
        )
        assert detected is not None
        question, options = detected
        assert question == "New prompt?"
        assert options == ["new-a", "new-b"]

    def test_detect_prompt_rejects_fragmented_option_block(self, orchestrator):
        detected = orchestrator._prompt_relay.detect_prompt_from_output(
            "Deploy now?\n1. yes\nlog: waiting for signal\n2. no\n"
        )
        assert detected is None

    def test_detect_prompt_requires_complete_question_option_structure(self, orchestrator):
        detected = orchestrator._prompt_relay.detect_prompt_from_output(
            "1. yes\n2. no\n"
        )
        assert detected is None

    def test_prompt_scan_dedupes_identical_output(self, orchestrator):
        child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WOWNER")
        orchestrator.children[42] = child
        with patch(
            "homebound.prompt_relay.read_child_output",
            new_callable=AsyncMock,
            return_value="Continue with migration?\n1. yes\n2. no\n",
        ):
            asyncio.run(orchestrator._prompt_relay.scan_runtime_prompts(orchestrator._poll_cycles))
            asyncio.run(orchestrator._prompt_relay.scan_runtime_prompts(orchestrator._poll_cycles))
        orchestrator._post.assert_awaited_once()

    def test_prompt_scan_replaces_prior_prompt_for_same_issue(self, orchestrator):
        child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WOWNER")
        orchestrator.children[42] = child
        orchestrator.config.prompt_relay.max_pending_per_issue = 1
        with patch(
            "homebound.prompt_relay.read_child_output",
            new_callable=AsyncMock,
            side_effect=[
                "Pick plan?\n1. basic\n2. pro\n",
                "Pick region?\n1. us-east-1\n2. us-west-2\n",
            ],
        ):
            asyncio.run(orchestrator._prompt_relay.scan_runtime_prompts(orchestrator._poll_cycles))
            asyncio.run(orchestrator._prompt_relay.scan_runtime_prompts(orchestrator._poll_cycles))
        prompts = orchestrator._prompt_relay.active_prompts_for_item(42)
        assert len(prompts) == 1
        assert prompts[0].question_text == "Pick region?"
        assert orchestrator._post.await_count == 2

    def test_resolve_prompt_answer_numeric_selector_variants(self, orchestrator):
        from homebound.orchestrator import PendingPrompt

        prompt = PendingPrompt(
            prompt_id="p-42-1",
            item_id=42,
            owner_user_id="WOWNER",
            question_text="Choose one?",
            options=["alpha", "beta", "gamma"],
            created_at=time.time(),
            last_seen_hash="hash-a",
        )
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "2")[0] == "beta"
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "2)")[0] == "beta"
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "2.")[0] == "beta"
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "2:")[0] == "beta"
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "9")[0] == "9"

    def test_resolve_prompt_answer_letter_selector_variants(self, orchestrator):
        from homebound.orchestrator import PendingPrompt

        prompt = PendingPrompt(
            prompt_id="p-42-1",
            item_id=42,
            owner_user_id="WOWNER",
            question_text="Choose one?",
            options=["alpha", "beta", "gamma"],
            created_at=time.time(),
            last_seen_hash="hash-a",
        )
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "B")[0] == "beta"
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "b)")[0] == "beta"
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "B.")[0] == "beta"
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "b:")[0] == "beta"

    def test_resolve_prompt_answer_exact_text_match(self, orchestrator):
        from homebound.orchestrator import PendingPrompt

        prompt = PendingPrompt(
            prompt_id="p-42-1",
            item_id=42,
            owner_user_id="WOWNER",
            question_text="Choose one?",
            options=["Use Staging", "Use Production"],
            created_at=time.time(),
            last_seen_hash="hash-a",
        )
        assert orchestrator._prompt_relay.resolve_prompt_answer(prompt, "use   production")[0] == "Use Production"

    def test_resolve_prompt_answer_non_matching_uses_free_text_fallback(self, orchestrator):
        from homebound.orchestrator import PendingPrompt

        prompt = PendingPrompt(
            prompt_id="p-42-1",
            item_id=42,
            owner_user_id="WOWNER",
            question_text="Choose one?",
            options=["alpha", "beta"],
            created_at=time.time(),
            last_seen_hash="hash-a",
        )
        value, error = orchestrator._prompt_relay.resolve_prompt_answer(prompt, "custom free text")
        assert value == "custom free text"
        assert error is None

    def test_explicit_prompt_answer_relays_to_child(self, orchestrator):
        from homebound.orchestrator import PendingPrompt
        from homebound.security import Principal

        orchestrator.config.security = SecurityConfig(
            allowed_users=["WOWNER", "WOTHER"], allow_open_channel=False
        )
        orchestrator.command_policy = orchestrator.command_policy.__class__(orchestrator.config.security)
        orchestrator._prompt_relay.command_policy = orchestrator.command_policy
        child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WOWNER")
        orchestrator.children[42] = child
        orchestrator._prompt_relay._pending_prompts[42] = [
            PendingPrompt(
                prompt_id="p-42-1",
                item_id=42,
                owner_user_id="WOWNER",
                question_text="Choose one?",
                options=["alpha", "beta"],
                created_at=time.time(),
                last_seen_hash="hash-a",
            )
        ]
        with patch("homebound.prompt_relay.send_to_child", new_callable=AsyncMock) as mock_send:
            asyncio.run(
                orchestrator._prompt_relay.handle_prompt_answer(
                    42, "2", Principal(user_id="WOTHER"), announce_denied=True,
                )
            )
        mock_send.assert_awaited_once()
        sent_message = mock_send.call_args.args[1]
        assert "beta" in sent_message
        assert 42 not in orchestrator._prompt_relay._pending_prompts

    def test_prompt_answer_denied_for_non_allowlisted_sender(self, orchestrator):
        from homebound.orchestrator import PendingPrompt
        from homebound.security import Principal

        orchestrator.config.security = SecurityConfig(
            allowed_users=["WALLOWED"], allow_open_channel=False
        )
        orchestrator.command_policy = orchestrator.command_policy.__class__(orchestrator.config.security)
        orchestrator._prompt_relay.command_policy = orchestrator.command_policy
        child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WOWNER")
        orchestrator.children[42] = child
        orchestrator._prompt_relay._pending_prompts[42] = [
            PendingPrompt(
                prompt_id="p-42-1",
                item_id=42,
                owner_user_id="WOWNER",
                question_text="Proceed?",
                options=["yes", "no"],
                created_at=time.time(),
                last_seen_hash="hash-a",
            )
        ]
        with patch("homebound.prompt_relay.send_to_child", new_callable=AsyncMock) as mock_send:
            handled = asyncio.run(
                orchestrator._prompt_relay.handle_prompt_answer(
                    42, "yes", Principal(user_id="WDENIED"), announce_denied=True,
                )
            )
        assert handled is True
        mock_send.assert_not_awaited()
        msg = orchestrator._post.call_args.args[0]
        assert "denied" in msg.lower()

    def test_prompt_ttl_expires(self, orchestrator):
        from homebound.orchestrator import PendingPrompt

        orchestrator.config.prompt_relay.ttl_seconds = 1
        now = time.time()
        orchestrator._prompt_relay._pending_prompts[42] = [
            PendingPrompt(
                prompt_id="p-42-1",
                item_id=42,
                owner_user_id="WOWNER",
                question_text="Proceed?",
                options=["yes", "no"],
                created_at=now - 10,
                last_seen_hash="hash-a",
                last_seen_at=now - 10,
            )
        ]
        orchestrator._prompt_relay.expire_pending_prompts()
        assert 42 not in orchestrator._prompt_relay._pending_prompts

    def test_poll_cycle_parses_explicit_prompt_answer_before_issue_routing(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage
        from homebound.orchestrator import PendingPrompt

        child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WOWNER")
        orchestrator.children[42] = child
        orchestrator._prompt_relay._pending_prompts[42] = [
            PendingPrompt(
                prompt_id="p-42-1",
                item_id=42,
                owner_user_id="WOWNER",
                question_text="Proceed?",
                options=["yes", "no"],
                created_at=time.time(),
                last_seen_hash="hash-a",
            )
        ]
        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="42 ans 1",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        with patch("homebound.prompt_relay.send_to_child", new_callable=AsyncMock) as mock_send:
            asyncio.run(orchestrator._poll_cycle())
        mock_send.assert_awaited_once()
        orchestrator._handle_issue_message.assert_not_awaited()

    def test_poll_cycle_treats_answer_word_as_normal_dev_issue_text(self, orchestrator):
        # "answer" as the first payload word must NOT trigger the prompt-reply path;
        # @Agent routing takes precedence and the full payload is passed as task text.
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent answer this question in detail",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_awaited_once_with(
            1,
            "answer this question in detail",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_auto_spawns_for_plain_text(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="implement feature for issue 42",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        # With smart routing, plain text auto-spawns a session
        orchestrator._handle_issue_message.assert_awaited_once()

    def test_poll_cycle_routes_agent_message_to_slot_one_chat(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent how many issues are open today?",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_awaited_once_with(
            1,
            "how many issues are open today?",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_agent_routes_to_free_slot(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent implement the login page",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        # @Agent <task> routes to next free slot (1)
        orchestrator._handle_issue_message.assert_awaited_once_with(
            1,
            "implement the login page",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_accepts_numbered_agent_command(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent2 77 fix flaky test",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        # Unified pool: @Agent2 <task> routes to slot 2, payload is the full task
        orchestrator._handle_issue_message.assert_awaited_once_with(
            2,
            "77 fix flaky test",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_agent_space_number_same_as_no_space(self, orchestrator):
        """@Agent 2 <task> should behave identically to @Agent2 <task>."""
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent 2 fix the tests",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_awaited_once_with(
            2,
            "fix the tests",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_rejects_invalid_agent_slot(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        max_slot = orchestrator.max_children
        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text=f"@Agent{max_slot + 1} check status",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_not_awaited()
        assert "slot must be between" in orchestrator._post.call_args.args[0].lower()

    def test_poll_cycle_rejects_invalid_dev_slot(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        max_slot = orchestrator.max_children
        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text=f"@Agent{max_slot + 1} gh 42 implement feature",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_not_awaited()
        assert "slot must be between" in orchestrator._post.call_args.args[0].lower()

    def test_poll_cycle_agent_space_number_routes_to_slot(self, orchestrator):
        # @Agent 1 <task> routes to slot 1 (space between Agent and number is ignored).
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent 1 implement feature",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_awaited_once_with(
            1,
            "implement feature",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_auto_spawns_for_unaddressed_text(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="what is the status",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        # With auto_spawn_on_no_match enabled, unaddressed text spawns a session
        orchestrator._handle_issue_message.assert_awaited_once()
        orchestrator._admin.handle_admin_query.assert_not_awaited()

    def test_poll_cycle_strips_claude_desktop_signature_for_admin_mention(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator.config.orchestrator.name = "homebound"
        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@homebound help *Sent using* @Claude",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._admin.handle_admin_query.assert_awaited_once_with(
            "help",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_strips_claude_desktop_signature_with_slack_mention_token(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator.config.orchestrator.name = "homebound"
        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@homebound help *Sent using* <@U123ABC>",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._admin.handle_admin_query.assert_awaited_once_with(
            "help",
            sender_user_id="WUSER",
            sender_extra={},
        )

    def test_poll_cycle_strips_claude_desktop_signature_for_unaddressed_text(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="do something useful\nSent using @Claude",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        # With auto_spawn_on_no_match, unaddressed text goes to auto-spawn
        orchestrator._handle_issue_message.assert_awaited_once()
        orchestrator._admin.handle_admin_query.assert_not_awaited()

    def test_poll_cycle_defaults_non_agent_mentions_to_auto_spawn(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@random-user check status",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        # With auto_spawn_on_no_match, non-role mentions auto-spawn a session
        orchestrator._handle_issue_message.assert_awaited_once()
        orchestrator._admin.handle_admin_query.assert_not_awaited()

    def test_poll_cycle_rejects_bare_agent_mention_with_usage_hint(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_not_awaited()
        orchestrator._admin.handle_admin_query.assert_not_awaited()
        msg = orchestrator._post.call_args.args[0]
        assert "@agent" in msg.lower() and "task" in msg.lower()

    def test_poll_cycle_rejects_bare_numbered_agent_mention_with_usage_hint(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@Agent2",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._handle_issue_message.assert_not_awaited()
        orchestrator._admin.handle_admin_query.assert_not_awaited()
        msg = orchestrator._post.call_args.args[0]
        assert "@agent" in msg.lower() and "task" in msg.lower()

    def test_poll_cycle_bare_help_handled_as_admin(self, orchestrator):
        """Bare 'help' without @homebound prefix should be handled as admin, not routed."""
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="help",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._admin.handle_admin_query.assert_awaited_once()
        orchestrator._handle_issue_message.assert_not_awaited()

    def test_poll_cycle_bare_status_handled_as_admin(self, orchestrator):
        """Bare 'status' without @homebound prefix should be handled as admin, not routed."""
        from homebound.adapters.transport import IncomingMessage

        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="status",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=False)
        orchestrator._health_check = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()
        orchestrator._handle_issue_message = AsyncMock()
        orchestrator._admin.handle_admin_query = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())

        orchestrator._admin.handle_admin_query.assert_awaited_once()
        orchestrator._handle_issue_message.assert_not_awaited()

    def test_poll_cycle_relays_only_latest_prompt_from_stacked_output(self, orchestrator):
        child = ChildInfo(item_id=42, window_name="AGENT-42", owner_user_id="WOWNER")
        orchestrator.children[42] = child
        orchestrator._retry_transport = AsyncMock(return_value=[])
        orchestrator._health_check = AsyncMock()
        with patch(
            "homebound.prompt_relay.read_child_output",
            new_callable=AsyncMock,
            return_value=(
                "Old prompt?\n1. old-a\n2. old-b\n"
                "info: continuing\n"
                "New prompt?\n1. new-a\n2. new-b\n"
            ),
        ):
            asyncio.run(orchestrator._poll_cycle())
        msg = orchestrator._post.call_args.args[0]
        assert "New prompt?" in msg
        assert "1. new-a" in msg
        assert "2. new-b" in msg
        assert "old-a" not in msg

    def test_poll_cycle_alias_triggers_admin_command(self):
        """@hb status should work the same as @homebound status when alias is configured."""
        from homebound.adapters.transport import IncomingMessage

        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        config.orchestrator.aliases = ["hb"]
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()

        orch._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="@hb status",
                    ts=str(time.time() + 1),
                    user="WUSER",
                    extra={},
                )
            ]
        )
        orch._transport = MagicMock()
        orch._transport.is_from_agent = MagicMock(return_value=False)
        orch._health_check = AsyncMock()
        orch._prompt_relay.scan_runtime_prompts = AsyncMock()
        orch._admin.handle_admin_query = AsyncMock()

        asyncio.run(orch._poll_cycle())

        orch._admin.handle_admin_query.assert_awaited_once()


class TestStartupVisibility:
    """Verify startup watchdog behavior for first-turn visibility."""

    @pytest.fixture
    def orchestrator(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=False)
            orch._post = AsyncMock()
            return orch

    def test_startup_watch_registered_after_spawn(self, orchestrator):
        child = ChildInfo(item_id=0, window_name="AGENT-0", owner_user_id="WUSER")
        with (
            patch("homebound.orchestrator.spawn_child", new_callable=AsyncMock, return_value=child),
            patch(
                "homebound.orchestrator.read_child_output",
                new_callable=AsyncMock,
                return_value="initial output",
            ),
        ):
            asyncio.run(orchestrator._handle_issue_message(0, "chat: first question", sender_user_id="WUSER"))
        assert 0 in orchestrator._startup_watch
        watch = orchestrator._startup_watch[0]
        assert watch.mode == "chat"
        assert watch.working_ping_sent is False
        assert watch.stuck_ping_sent is False

    def test_working_ping_fires_once_when_no_signal(self, orchestrator):
        from homebound.orchestrator import StartupWatch

        item_id = 5
        child = ChildInfo(item_id=item_id, window_name="AGENT-5")
        orchestrator.children[item_id] = child
        baseline_hash = orchestrator._hash_output("same output")
        orchestrator._startup_watch[item_id] = StartupWatch(
            started_at=time.time() - 31,
            mode="chat",
            baseline_output_hash=baseline_hash,
        )
        with (
            patch(
                "homebound.orchestrator.read_child_output",
                new_callable=AsyncMock,
                return_value="same output",
            ),
            patch("homebound.orchestrator.send_to_child", new_callable=AsyncMock) as mock_send,
        ):
            asyncio.run(orchestrator._check_startup_visibility())
            asyncio.run(orchestrator._check_startup_visibility())
        mock_send.assert_not_awaited()
        assert orchestrator._post.await_count == 1
        assert "still working on the initial request" in orchestrator._post.call_args.args[0]

    def test_stuck_ping_fires_once_when_no_signal_persists(self, orchestrator):
        from homebound.orchestrator import StartupWatch

        item_id = 6
        child = ChildInfo(item_id=item_id, window_name="AGENT-6")
        orchestrator.children[item_id] = child
        baseline_hash = orchestrator._hash_output("same output")
        orchestrator._startup_watch[item_id] = StartupWatch(
            started_at=time.time() - 181,
            mode="chat",
            baseline_output_hash=baseline_hash,
        )
        with patch(
            "homebound.orchestrator.read_child_output",
            new_callable=AsyncMock,
            return_value="same output",
        ):
            asyncio.run(orchestrator._check_startup_visibility())
            asyncio.run(orchestrator._check_startup_visibility())
        assert orchestrator._post.await_count == 1
        assert "no visible update yet" in orchestrator._post.call_args.args[0]

    def test_stuck_ping_for_slot_uses_status_hint(self, orchestrator):
        from homebound.orchestrator import StartupWatch

        item_id = 1
        child = ChildInfo(item_id=item_id, window_name="AGENT-1")
        orchestrator.children[item_id] = child
        baseline_hash = orchestrator._hash_output("same output")
        orchestrator._startup_watch[item_id] = StartupWatch(
            started_at=time.time() - 181,
            mode="chat",
            baseline_output_hash=baseline_hash,
        )
        with patch(
            "homebound.orchestrator.read_child_output",
            new_callable=AsyncMock,
            return_value="same output",
        ):
            asyncio.run(orchestrator._check_startup_visibility())
        msg = orchestrator._post.call_args.args[0]
        assert f"use `@{orchestrator.config.name} status`".lower() in msg.lower()

    def test_startup_watch_clears_on_agent_signal(self, orchestrator):
        from homebound.orchestrator import StartupWatch

        orchestrator._startup_watch[7] = StartupWatch(started_at=time.time(), mode="chat")
        orchestrator._record_agent_startup_signal("[agent-7] posted update")
        assert 7 not in orchestrator._startup_watch

    def test_startup_watch_clears_on_dev_agent_signal(self, orchestrator):
        from homebound.orchestrator import StartupWatch

        item_id = 2
        orchestrator._startup_watch[item_id] = StartupWatch(started_at=time.time(), mode="chat")
        orchestrator._record_agent_startup_signal("[agent-2] posted update")
        assert item_id not in orchestrator._startup_watch

    def test_startup_watch_not_cleared_by_orchestrator_status_message(self, orchestrator):
        from homebound.orchestrator import StartupWatch

        orchestrator._startup_watch[7] = StartupWatch(started_at=time.time(), mode="chat")
        orchestrator._record_agent_startup_signal("[homebound] Dev7: New session started (chat).")
        assert 7 in orchestrator._startup_watch

    def test_startup_watch_clears_on_tmux_output_change(self, orchestrator):
        from homebound.orchestrator import StartupWatch

        item_id = 8
        child = ChildInfo(item_id=item_id, window_name="AGENT-8")
        orchestrator.children[item_id] = child
        orchestrator._startup_watch[item_id] = StartupWatch(
            started_at=time.time() - 31,
            mode="chat",
            baseline_output_hash=orchestrator._hash_output("old output"),
        )
        with patch(
            "homebound.orchestrator.read_child_output",
            new_callable=AsyncMock,
            return_value="new output",
        ):
            asyncio.run(orchestrator._check_startup_visibility())
        assert item_id not in orchestrator._startup_watch
        orchestrator._post.assert_not_awaited()

    def test_poll_cycle_agent_message_clears_watch_even_if_sender_denied(self, orchestrator):
        from homebound.adapters.transport import IncomingMessage
        from homebound.orchestrator import StartupWatch

        orchestrator.config.security = SecurityConfig(
            allowed_users=["WADMIN"], allow_open_channel=False,
        )
        orchestrator.command_policy = orchestrator.command_policy.__class__(orchestrator.config.security)
        orchestrator._startup_watch[9] = StartupWatch(started_at=time.time(), mode="chat")
        orchestrator._retry_transport = AsyncMock(
            return_value=[
                IncomingMessage(
                    text="[agent-9] posted first answer",
                    ts=str(time.time() + 1),
                    user="",
                    extra={"bot_id": "B123"},
                )
            ]
        )
        orchestrator._transport = MagicMock()
        orchestrator._transport.is_from_agent = MagicMock(return_value=True)
        orchestrator._health_check = AsyncMock()
        orchestrator._check_startup_visibility = AsyncMock()
        orchestrator._prompt_relay.scan_runtime_prompts = AsyncMock()

        asyncio.run(orchestrator._poll_cycle())
        assert 9 not in orchestrator._startup_watch


class TestIssueRouting:
    """Verify issue message routing (spawn, follow-up, close)."""

    @pytest.fixture
    def orchestrator(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
            return orch

    def test_task_routing_in_dry_run(self, orchestrator):
        asyncio.run(
            orchestrator._handle_issue_message(215, "implement feature", sender_user_id="WUSER")
        )
        orchestrator._post.assert_called_once()
        msg = orchestrator._post.call_args.args[0]
        assert "task" in msg.lower() or "session" in msg.lower()

    def test_freeform_routing_in_dry_run(self, orchestrator):
        asyncio.run(
            orchestrator._handle_issue_message(215, "freeform: check test output", sender_user_id="WUSER")
        )
        orchestrator._post.assert_called_once()
        msg = orchestrator._post.call_args.args[0]
        assert "freeform" in msg.lower()

    def test_chat_routing_in_dry_run(self, orchestrator):
        asyncio.run(
            orchestrator._handle_issue_message(215, "chat: check output", sender_user_id="WUSER")
        )
        orchestrator._post.assert_called_once()
        msg = orchestrator._post.call_args.args[0]
        assert "chat" in msg.lower()

    def test_close_no_session(self, orchestrator):
        asyncio.run(
            orchestrator._handle_issue_message(999, "close", sender_user_id="WUSER")
        )
        msg = orchestrator._post.call_args.args[0]
        assert "no active session" in msg.lower()

    def test_spawn_honors_mode_keyword_prefix(self, orchestrator):
        child = ChildInfo(
            item_id=1,
            window_name="AGENT-1",
            owner_user_id="WUSER",
        )
        orchestrator.dry_run = False
        with (
            patch(
                "homebound.orchestrator.spawn_child",
                new_callable=AsyncMock,
                return_value=child,
            ) as mock_spawn,
            patch(
                "homebound.orchestrator.read_child_output",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            asyncio.run(
                orchestrator._handle_issue_message(
                    1,
                    "freeform: do some work",
                    sender_user_id="WUSER",
                )
            )

        assert mock_spawn.await_count == 1
        assert mock_spawn.await_args.kwargs["mode"] == "freeform"


class TestAtAgentCascadeRouting:
    """Verify @Agent <task> (no slot) routes via the Tier 2-4 cascade."""

    def test_at_agent_no_slot_routes_via_cascade(self):
        """@Agent <task> should route to existing session via keyword match."""
        from homebound.adapters.transport import IncomingMessage
        from homebound.config import RoutingConfig
        from homebound.orchestrator import Orchestrator

        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(keyword_match_threshold=1),
        )
        orch = Orchestrator(config=config, dry_run=True)
        orch._post = AsyncMock()
        orch.startup_ts = 0.0  # Accept all messages regardless of ts

        # Set up an existing session with keywords
        child = ChildInfo(item_id=1, window_name="AGENT-1", owner_user_id="U123")
        child.recent_keywords = ["sectors", "india", "economy", "fii"]
        orch.children[1] = child

        future_ts = str(time.time() + 1000)

        # Mock send_to_child so we can verify routing
        with patch("homebound.orchestrator.send_to_child", new_callable=AsyncMock) as mock_send:
            mock_transport = MagicMock()
            mock_transport.poll = MagicMock(return_value=[
                IncomingMessage(
                    text="@Agent which sectors in india have better prospects",
                    ts=future_ts,
                    user="U123",
                ),
            ])
            mock_transport.is_from_agent = MagicMock(return_value=False)
            mock_transport.poll_thread_replies = MagicMock(return_value=[])
            orch._transport = mock_transport

            asyncio.run(orch._poll_cycle())

            # Should have routed to child 1 via keyword match, not spawned a new session
            mock_send.assert_called_once()
            assert mock_send.call_args.args[0] is child


class TestRetryTransport:
    """Verify _retry_transport attempt/retry count semantics."""

    def _make_orchestrator(self, max_retries: int):
        from homebound.config import SessionsConfig
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            sessions=SessionsConfig(max_retries=max_retries),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        orch._post = AsyncMock()
        return orch

    def test_zero_retries_makes_exactly_one_attempt(self):
        orch = self._make_orchestrator(max_retries=0)
        calls = 0

        def failing_call():
            nonlocal calls
            calls += 1
            raise RuntimeError("transport error")

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError):
            asyncio.run(orch._retry_transport(failing_call, "test"))
        assert calls == 1

    def test_one_retry_makes_exactly_two_attempts(self):
        orch = self._make_orchestrator(max_retries=1)
        calls = 0

        def failing_call():
            nonlocal calls
            calls += 1
            raise RuntimeError("transport error")

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError):
            asyncio.run(orch._retry_transport(failing_call, "test"))
        assert calls == 2

    def test_succeeds_on_first_attempt_without_retry(self):
        orch = self._make_orchestrator(max_retries=3)
        calls = 0

        def succeeding_call():
            nonlocal calls
            calls += 1
            return "ok"

        result = asyncio.run(orch._retry_transport(succeeding_call, "test"))
        assert result == "ok"
        assert calls == 1

    def test_succeeds_on_second_attempt(self):
        orch = self._make_orchestrator(max_retries=3)
        calls = 0

        def flaky_call():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise RuntimeError("transient error")
            return "ok"

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = asyncio.run(orch._retry_transport(flaky_call, "test"))
        assert result == "ok"
        assert calls == 2


class TestDurationFormat:
    """Verify duration formatting."""

    def test_seconds(self):
        from homebound.admin import format_duration
        assert format_duration(30) == "30s"

    def test_minutes(self):
        from homebound.admin import format_duration
        assert format_duration(300) == "5m"

    def test_hours(self):
        from homebound.admin import format_duration
        assert format_duration(3700) == "1h01m"


class TestSkillsCommand:
    """Verify skills admin command and list_custom_skills utility."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path):
        """Prevent tests from picking up real ~/.claude/skills/."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        with patch("homebound.session.Path.home", return_value=fake_home):
            yield fake_home

    def test_list_custom_skills_parses_frontmatter(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: foo\ndescription: Does foo things\n---\n\nBody here.\n"
        )
        result = list_custom_skills(tmp_path)
        assert result == [("foo", "Does foo things")]

    def test_list_custom_skills_multiple_sorted(self, tmp_path):
        skills_root = tmp_path / ".claude" / "skills"
        for name, desc in [("beta", "B skill"), ("alpha", "A skill")]:
            d = skills_root / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n")
        result = list_custom_skills(tmp_path)
        assert result == [("alpha", "A skill"), ("beta", "B skill")]

    def test_list_custom_skills_empty_dir(self, tmp_path):
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        result = list_custom_skills(tmp_path)
        assert result == []

    def test_list_custom_skills_missing_dir(self, tmp_path):
        result = list_custom_skills(tmp_path)
        assert result == []

    def test_list_custom_skills_ignores_symlinks(self, tmp_path):
        skills_root = tmp_path / ".claude" / "skills"
        real_dir = tmp_path / "real_skill"
        real_dir.mkdir(parents=True)
        (real_dir / "SKILL.md").write_text("---\nname: real\ndescription: Real\n---\n")
        skills_root.mkdir(parents=True, exist_ok=True)
        (skills_root / "linked").symlink_to(real_dir)
        result = list_custom_skills(tmp_path)
        assert result == []

    def test_frontmatter_with_dashes_in_value(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "dashy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: dashy\ndescription: does bar---baz things\n---\n"
        )
        result = list_custom_skills(tmp_path)
        assert result == [("dashy", "does bar---baz things")]

    def test_list_custom_skills_includes_user_level(self, tmp_path, _isolate_home):
        """User-level skills from ~/.claude/skills/ are included."""
        user_skill = _isolate_home / ".claude" / "skills" / "global"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text(
            "---\nname: global\ndescription: User-level skill\n---\n"
        )
        result = list_custom_skills(tmp_path)
        assert result == [("global", "User-level skill")]

    def test_list_custom_skills_project_overrides_user(self, tmp_path, _isolate_home):
        """Project-level skills take precedence over user-level with same name."""
        user_skill = _isolate_home / ".claude" / "skills" / "dupe"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text(
            "---\nname: dupe\ndescription: User version\n---\n"
        )
        proj_skill = tmp_path / ".claude" / "skills" / "dupe"
        proj_skill.mkdir(parents=True)
        (proj_skill / "SKILL.md").write_text(
            "---\nname: dupe\ndescription: Project version\n---\n"
        )
        result = list_custom_skills(tmp_path)
        assert result == [("dupe", "Project version")]

    @pytest.fixture
    def orchestrator(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
            return orch

    def test_skills_command_posts_list(self, orchestrator):
        with patch(
            "homebound.admin.list_custom_skills",
            return_value=[("commit", "Create a git commit"), ("review", "Review code")],
        ):
            asyncio.run(orchestrator._admin.handle_admin_query("skills", sender_user_id="WUSER"))
        orchestrator._post.assert_awaited_once()
        msg = orchestrator._post.call_args.args[0]
        assert "Available skills" in msg and "(2)" in msg
        assert "`/commit`" in msg
        assert "`/review`" in msg

    def test_skills_command_empty(self, orchestrator):
        with patch("homebound.admin.list_custom_skills", return_value=[]):
            asyncio.run(orchestrator._admin.handle_admin_query("skills", sender_user_id="WUSER"))
        orchestrator._post.assert_awaited_once()
        msg = orchestrator._post.call_args.args[0]
        assert "No custom skills found" in msg

    def test_skills_command_denied_when_unauthorized(self):
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=False)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock()
        with patch("homebound.admin.list_custom_skills", return_value=[("foo", "bar")]):
            asyncio.run(orch._admin.handle_admin_query("skills", sender_user_id="WUSER"))
        orch._post.assert_not_awaited()


# ---------------------------------------------------------------------------
# Transport resilience: adaptive backoff + recovery notification
# ---------------------------------------------------------------------------

class TestTransportResilience:
    """Verify consecutive failure tracking, adaptive backoff, and recovery notification."""

    def _make_orchestrator(self, **session_kwargs):
        from homebound.config import SessionsConfig
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        config.sessions = SessionsConfig(**session_kwargs)
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock(return_value="")
        return orch

    def test_consecutive_failure_counter_increments(self):
        orch = self._make_orchestrator()
        asyncio.run(orch._update_transport_health(False))
        assert orch._consecutive_poll_failures == 1
        asyncio.run(orch._update_transport_health(False))
        assert orch._consecutive_poll_failures == 2

    def test_failure_counter_resets_on_success(self):
        orch = self._make_orchestrator()
        orch._consecutive_poll_failures = 5
        orch._outage_start_time = time.time() - 60
        asyncio.run(orch._update_transport_health(True))
        assert orch._consecutive_poll_failures == 0
        assert orch._outage_start_time is None

    def test_recovery_notification_after_outage(self):
        orch = self._make_orchestrator(outage_threshold=3)
        orch._consecutive_poll_failures = 5
        orch._outage_start_time = time.time() - 120
        asyncio.run(orch._update_transport_health(True))
        orch._post.assert_awaited_once()
        msg = orch._post.call_args.args[0]
        assert "back online" in msg
        assert "5 poll cycles failed" in msg

    def test_no_recovery_notification_below_threshold(self):
        orch = self._make_orchestrator(outage_threshold=3)
        orch._consecutive_poll_failures = 1
        asyncio.run(orch._update_transport_health(True))
        orch._post.assert_not_awaited()
        assert orch._consecutive_poll_failures == 0

    def test_outage_start_time_set_on_first_failure(self):
        orch = self._make_orchestrator()
        assert orch._outage_start_time is None
        asyncio.run(orch._update_transport_health(False))
        assert orch._outage_start_time is not None

    def test_effective_poll_delay_normal(self):
        orch = self._make_orchestrator(poll_interval=10, outage_threshold=3)
        orch._consecutive_poll_failures = 0
        delay = orch._effective_poll_delay()
        assert 10 <= delay <= 11  # base + up to 1s jitter

    def test_effective_poll_delay_escalates(self):
        orch = self._make_orchestrator(
            poll_interval=10, outage_threshold=3, outage_max_interval=120,
        )
        # At threshold: 10 * 2^1 = 20
        orch._consecutive_poll_failures = 3
        delay = orch._effective_poll_delay()
        assert 20 <= delay <= 22  # 20 + up to 10% jitter

        # One step past: 10 * 2^2 = 40
        orch._consecutive_poll_failures = 4
        delay = orch._effective_poll_delay()
        assert 40 <= delay <= 44

    def test_effective_poll_delay_caps_at_max(self):
        orch = self._make_orchestrator(
            poll_interval=10, outage_threshold=3, outage_max_interval=120,
        )
        orch._consecutive_poll_failures = 100
        delay = orch._effective_poll_delay()
        assert delay <= 120 * 1.1  # max + 10% jitter


class TestSleepGapRecovery:
    """Verify _last_poll_ts prevents message loss after macOS sleep."""

    def _make_orchestrator(self):
        from homebound.config import SessionsConfig
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        config.sessions = SessionsConfig()
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=True)
            orch._post = AsyncMock(return_value="")
        return orch

    def test_since_ts_uses_last_poll_ts_after_sleep_gap(self):
        """After a sleep gap, since_ts should reach back to _last_poll_ts."""
        orch = self._make_orchestrator()
        lookback = orch.config.transport.lookback_minutes  # default 5

        # Simulate: last successful poll was 20 minutes ago
        twenty_min_ago = time.time() - 20 * 60
        orch._last_poll_ts = twenty_min_ago

        # The fixed lookback would give ~5 min ago, but min() should pick _last_poll_ts
        since_ts = min(orch._last_poll_ts, time.time() - (lookback * 60))

        # since_ts should be approximately 20 min ago (within 1s tolerance)
        assert abs(since_ts - twenty_min_ago) < 1.0
        # And it should be much older than the 5-min lookback
        five_min_ago = time.time() - 5 * 60
        assert since_ts < five_min_ago

    def test_last_poll_ts_updates_on_successful_poll(self):
        """_last_poll_ts should advance after a successful poll cycle."""
        orch = self._make_orchestrator()

        # Set _last_poll_ts to 10 minutes ago
        orch._last_poll_ts = time.time() - 10 * 60

        # Mock transport.poll to return empty list (success)
        mock_transport = MagicMock()
        mock_transport.poll.return_value = []
        orch._transport = mock_transport

        before = time.time()
        asyncio.run(orch._poll_cycle())
        after = time.time()

        # _last_poll_ts should have been updated to approximately now
        assert orch._last_poll_ts >= before
        assert orch._last_poll_ts <= after

    def test_last_poll_ts_preserved_on_failed_poll(self):
        """_last_poll_ts should NOT advance when transport fails."""
        orch = self._make_orchestrator()

        old_ts = time.time() - 10 * 60
        orch._last_poll_ts = old_ts

        # Mock transport.poll to fail
        mock_transport = MagicMock()
        mock_transport.poll.side_effect = RuntimeError("network down")
        orch._transport = mock_transport

        asyncio.run(orch._poll_cycle())

        # _last_poll_ts should remain unchanged
        assert orch._last_poll_ts == old_ts


# ---------------------------------------------------------------------------
# Non-blocking spawn: background asyncio.Task for session init
# ---------------------------------------------------------------------------

class TestBackgroundSpawn:
    """Verify spawn_child runs as a background task, not blocking the poll loop."""

    def _make_orchestrator(self):
        from homebound.config import SessionsConfig
        config = HomeboundConfig()
        config.security = SecurityConfig(allow_open_channel=True)
        config.sessions = SessionsConfig()
        with patch("homebound.orchestrator.Orchestrator.transport", create=True):
            from homebound.orchestrator import Orchestrator
            orch = Orchestrator(config=config, dry_run=False)
            orch._post = AsyncMock(return_value="")
            orch._save_children_state = MagicMock()
        return orch

    def test_spawn_does_not_block_poll_cycle(self):
        """_handle_issue_message should return immediately while spawn runs in background."""
        orch = self._make_orchestrator()

        slow_child = ChildInfo(item_id=42, window_name="agent-42")

        async def slow_spawn(*args, **kwargs):
            await asyncio.sleep(5)
            return slow_child

        async def run():
            with (
                patch("homebound.orchestrator.spawn_child", side_effect=slow_spawn),
                patch("homebound.orchestrator.read_child_output", new_callable=AsyncMock, return_value=""),
            ):
                start = asyncio.get_event_loop().time()
                await orch._handle_issue_message(42, "do something", sender_user_id="U1")
                elapsed = asyncio.get_event_loop().time() - start

                # Should return almost immediately (< 1s), not wait 5s
                assert elapsed < 1.0
                # Sentinel should be set
                assert 42 in orch.children
                assert orch.children[42] is None

                # Now let the background task complete
                assert len(orch._spawn_tasks) == 1
                task = next(iter(orch._spawn_tasks))
                await task

                # Child should now be populated
                assert orch.children[42] is slow_child
                assert slow_child.owner_user_id == "U1"

        asyncio.run(run())

    def test_spawn_failure_cleans_up_sentinel(self):
        """When spawn_child raises, the sentinel should be cleaned up."""
        orch = self._make_orchestrator()

        async def failing_spawn(*args, **kwargs):
            raise RuntimeError("tmux exploded")

        async def run():
            with patch("homebound.orchestrator.spawn_child", side_effect=failing_spawn):
                await orch._handle_issue_message(42, "do something", sender_user_id="U1")

                # Sentinel set
                assert 42 in orch.children

                # Let the background task complete
                task = next(iter(orch._spawn_tasks))
                await task

                # Sentinel should be cleaned up
                assert 42 not in orch.children

                # Error should have been posted
                error_calls = [
                    c for c in orch._post.call_args_list
                    if "Failed to spawn" in str(c)
                ]
                assert len(error_calls) == 1

        asyncio.run(run())

    def test_shutdown_awaits_in_flight_spawns(self):
        """_shutdown should wait for (and cancel if needed) in-flight spawn tasks."""
        orch = self._make_orchestrator()

        async def very_slow_spawn(*args, **kwargs):
            await asyncio.sleep(60)
            return ChildInfo(item_id=99, window_name="agent-99")

        async def run():
            with (
                patch("homebound.orchestrator.spawn_child", side_effect=very_slow_spawn),
            ):
                await orch._handle_issue_message(99, "do something", sender_user_id="U1")
                assert len(orch._spawn_tasks) == 1
                task = next(iter(orch._spawn_tasks))
                assert not task.done()

                # Shutdown should wait then cancel
                await orch._shutdown()
                assert task.done()

        asyncio.run(run())
