"""Tests for child mode routing (task vs freeform vs chat)."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, patch

import pytest

from homebound.config import HomeboundConfig


class TestChildModeRouting:
    """Verify spawn_child() generates correct prompts based on mode."""

    @pytest.fixture
    def mock_tmux(self):
        with (
            patch("homebound.session.run_tmux", new_callable=AsyncMock) as mock_run,
            patch("homebound.session.wait_for_prompt", new_callable=AsyncMock) as mock_wait,
            patch("homebound.session.send_keys", new_callable=AsyncMock) as mock_send,
            patch("homebound.session.new_window", new_callable=AsyncMock) as mock_new_win,
        ):
            mock_run.return_value = (0, "", "")
            mock_wait.return_value = True
            mock_send.return_value = True
            yield {
                "run_tmux": mock_run,
                "wait_for_prompt": mock_wait,
                "send_keys": mock_send,
                "new_window": mock_new_win,
            }

    def test_task_mode_sends_structured_prompt(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        child = asyncio.run(spawn_child(215, "implement sector overlay", config, mode="task"))

        mock_send = mock_tmux["send_keys"]
        mock_send.assert_called_once()
        _, sent_prompt = mock_send.call_args.args
        assert "Work on item Agent215" in sent_prompt
        assert child.item_id == 215
        assert child.window_name == "AGENT-215"

    def test_freeform_mode_sends_legacy_prompt(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        asyncio.run(spawn_child(215, "check test output", config, mode="freeform"))

        mock_send = mock_tmux["send_keys"]
        mock_send.assert_called_once()
        _, sent_prompt = mock_send.call_args.args
        assert "Agent215" in sent_prompt
        assert "BEGIN TASK" in sent_prompt
        assert "check test output" in sent_prompt
        assert "COMMUNICATION RULES" in sent_prompt
        assert "Work on item Agent" not in sent_prompt

    def test_default_mode_is_task(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        asyncio.run(spawn_child(100, "some task", config))

        mock_send = mock_tmux["send_keys"]
        _, sent_prompt = mock_send.call_args.args
        assert "Work on item Agent100" in sent_prompt

    def test_task_prompt_ignores_task_text(self, mock_tmux):
        from homebound.session import spawn_child

        config = HomeboundConfig()
        asyncio.run(spawn_child(42, "this long task description should not appear", config, mode="task"))

        mock_send = mock_tmux["send_keys"]
        _, sent_prompt = mock_send.call_args.args
        assert "this long task description" not in sent_prompt
        assert "Work on item Agent42" in sent_prompt


class TestModeKeywordDetection:
    """Verify keyword-based mode detection."""

    def test_freeform_keyword_triggers(self):
        config = HomeboundConfig()
        freeform = config.modes.get("freeform")
        assert freeform is not None
        task_text = "freeform: just check the test output"
        assert task_text.lower().startswith(freeform.keyword)

    def test_chat_keyword_triggers(self):
        config = HomeboundConfig()
        chat = config.modes.get("chat")
        assert chat is not None
        task_text = "chat: check the output"
        assert task_text.lower().startswith(chat.keyword)

    def test_normal_task_does_not_trigger_keywords(self):
        config = HomeboundConfig()
        for task in [
            "implement the sector overlay",
            "fix the broken tests",
            "add freeform text input to the UI",
        ]:
            for mode_name, mode_cfg in config.modes.items():
                if mode_cfg.keyword:
                    assert not task.lower().startswith(mode_cfg.keyword), (
                        f"'{task}' should NOT match {mode_name} keyword"
                    )


class TestAgentPrefixFiltering:
    """Verify ignored prefixes prevent re-routing loops."""

    def test_name_and_role_prefixes_in_ignored(self):
        config = HomeboundConfig()
        prefixes = config.ignored_prefixes
        assert config.name in prefixes
        assert "agent-" in prefixes

    def test_agent_messages_filtered(self):
        """Slack messages from known agents must be caught."""
        config = HomeboundConfig()
        prefixes = config.ignored_prefixes

        agent_messages = [
            f"[{config.name}] Agent215: New session started.",
            "[agent-42] Done.",
            "[agent-1] Still working.",
        ]
        for text in agent_messages:
            matched = any(f"[{prefix}" in text for prefix in prefixes)
            assert matched, f"Message should be filtered: {text}"

    def test_human_messages_pass(self):
        config = HomeboundConfig()
        prefixes = config.ignored_prefixes

        human_messages = [
            "gh 215 implement sector overlay",
            "gh 178 freeform: check test output",
        ]
        for text in human_messages:
            matched = any(f"[{prefix}" in text for prefix in prefixes)
            assert not matched, f"Human message should NOT be filtered: {text}"


class TestAdminPattern:
    """Verify admin command pattern anchoring."""

    def test_admin_pattern_anchored(self):
        config = HomeboundConfig()
        pattern = config.admin_pattern
        assert re.search(pattern, f"@{config.name} status", re.IGNORECASE)
        assert re.search(pattern, f"hey @{config.name} status", re.IGNORECASE) is None
