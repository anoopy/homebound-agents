"""Tests for YAML configuration loading and defaults."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from homebound.config import (
    HomeboundConfig,
    ModeConfig,
    OrchestratorConfig,
    PromptRelayConfig,
    RuntimeConfig,
    SecurityConfig,
    SessionsConfig,
    TrackerConfig,
    TransportConfig,
    load_config,
    _parse_config,
)


class TestHomeboundConfigDefaults:
    """Verify default configuration values."""

    def test_default_orchestrator_name(self):
        config = HomeboundConfig()
        assert config.name == "homebound"

    def test_default_tmux_session_name(self):
        config = HomeboundConfig()
        assert config.tmux_session_name == "homebound"

    def test_default_mode_is_task(self):
        config = HomeboundConfig()
        assert config.default_mode == "task"

    def test_default_modes_populated(self):
        config = HomeboundConfig()
        assert "task" in config.modes
        assert "freeform" in config.modes
        assert "chat" in config.modes

    def test_task_mode_prompt_template(self):
        config = HomeboundConfig()
        assert "Work on item " in config.modes["task"].prompt_template
        assert "{work_item_label}" in config.modes["task"].prompt_template

    def test_freeform_keyword(self):
        config = HomeboundConfig()
        assert config.modes["freeform"].keyword == "freeform:"

    def test_chat_keyword(self):
        config = HomeboundConfig()
        assert config.modes["chat"].keyword == "chat:"

    def test_default_close_commands(self):
        config = HomeboundConfig()
        assert "close" in config.close_commands
        assert "stop" in config.close_commands
        assert "done" in config.close_commands
        assert "exit" in config.close_commands

    def test_default_max_concurrent(self):
        config = HomeboundConfig()
        assert config.sessions.max_concurrent == 5

    def test_default_idle_timeout(self):
        config = HomeboundConfig()
        assert config.sessions.idle_timeout == 1800

    def test_default_runtime_is_claude_code(self):
        config = HomeboundConfig()
        assert config.runtimes["agent"].type == "claude-code"
        assert config.runtimes["agent"].command == "claude"

    def test_default_runtime_safe(self):
        """Default runtime command must NOT contain --dangerously-skip-permissions."""
        config = HomeboundConfig()
        assert "--dangerously-skip-permissions" not in config.runtimes["agent"].command

    def test_default_prompt_relay_enabled(self):
        config = HomeboundConfig()
        assert isinstance(config.prompt_relay, PromptRelayConfig)
        assert config.prompt_relay.enabled is True


class TestIgnoredPrefixes:
    """Verify ignored prefix computation."""

    def test_includes_name_and_agent_prefixes(self):
        config = HomeboundConfig()
        prefixes = config.ignored_prefixes
        assert "homebound" in prefixes
        assert "agent-" in prefixes
        assert "Agent" in prefixes

    def test_custom_ignored_prefixes(self):
        config = HomeboundConfig()
        config.transport.ignored_prefixes = ["my-bot"]
        prefixes = config.ignored_prefixes
        assert "my-bot" in prefixes
        assert "homebound" in prefixes
        assert "agent-" in prefixes
        assert "Agent" in prefixes


class TestAdminPattern:
    """Verify admin pattern template substitution."""

    def test_default_admin_pattern(self):
        config = HomeboundConfig()
        assert re.search(config.admin_pattern, "@homebound status", re.IGNORECASE)

    def test_custom_name_admin_pattern(self):
        config = HomeboundConfig()
        config.orchestrator.name = "my-bot"
        assert re.search(config.admin_pattern, "@my-bot status", re.IGNORECASE)

    def test_alias_matches_admin_pattern(self):
        config = HomeboundConfig()
        config.orchestrator.aliases = ["hb"]
        pattern = config.admin_pattern
        assert re.search(pattern, "@homebound status", re.IGNORECASE)
        assert re.search(pattern, "@hb status", re.IGNORECASE)

    def test_alias_included_in_ignored_prefixes(self):
        config = HomeboundConfig()
        config.orchestrator.aliases = ["hb", "h"]
        prefixes = config.ignored_prefixes
        assert "hb" in prefixes
        assert "h" in prefixes

    def test_no_aliases_by_default(self):
        config = HomeboundConfig()
        assert config.orchestrator.aliases == []
        pattern = config.admin_pattern
        assert re.search(pattern, "@homebound status", re.IGNORECASE)


class TestYAMLLoading:
    """Verify YAML file loading and parsing."""

    def test_load_missing_file_returns_defaults(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.name == "homebound"

    def test_load_from_yaml(self, tmp_path):
        yaml_path = tmp_path / "homebound.yaml"
        yaml_path.write_text(
            "orchestrator:\n"
            "  name: my-lead\n"
            "transport:\n"
            "  type: slack\n"
            "  channel_id: C123\n"
            "tracker:\n"
            "  project_dir: /tmp/project\n"
            "sessions:\n"
            "  max_concurrent: 3\n"
        )
        config = load_config(yaml_path)
        assert config.name == "my-lead"
        assert config.transport.channel_id == "C123"
        assert config.sessions.max_concurrent == 3

    def test_parse_empty_yaml(self):
        config = _parse_config({})
        assert config.name == "homebound"

    def test_parse_modes(self):
        raw = {
            "modes": {
                "default": "task",
                "task": {
                    "prompt_template": "do stuff {item_id}",
                },
                "custom": {
                    "keyword": "custom:",
                    "prompt_template": "custom {task_text}",
                },
            }
        }
        config = _parse_config(raw)
        assert "task" in config.modes
        assert "custom" in config.modes
        assert config.modes["custom"].keyword == "custom:"

    def test_parse_close_commands(self):
        raw = {"close_commands": ["bye", "quit"]}
        config = _parse_config(raw)
        assert "bye" in config.close_commands
        assert "quit" in config.close_commands

    def test_parse_security(self):
        raw = {
            "security": {
                "allowed_users": ["WFAKE01", "WFAKE02"],
                "destructive_confirm_timeout": 30,
            }
        }
        config = _parse_config(raw)
        assert config.security.allowed_users == ["WFAKE01", "WFAKE02"]
        assert config.security.destructive_confirm_timeout == 30

    def test_parse_security_boolean_flags(self):
        raw = {
            "security": {
                "allow_open_channel": True,
                "allow_bots": False,
                "allow_admin_takeover": True,
            }
        }
        config = _parse_config(raw)
        assert config.security.allow_open_channel is True
        assert config.security.allow_bots is False
        assert config.security.allow_admin_takeover is True

    def test_parse_security_rejects_non_bool_flags(self):
        raw = {
            "security": {
                "allow_open_channel": "false",
            }
        }
        with pytest.raises(TypeError, match="allow_open_channel must be a bool"):
            _parse_config(raw)

    def test_parse_security_normalizes_allowed_users(self):
        raw = {
            "security": {
                "allowed_users": [" WFAKE01 ", "WFAKE02"],
            }
        }
        config = _parse_config(raw)
        assert config.security.allowed_users == ["WFAKE01", "WFAKE02"]

    def test_parse_security_rejects_non_string_allowed_user(self):
        raw = {
            "security": {
                "allowed_users": ["WFAKE01", 123],
            }
        }
        with pytest.raises(TypeError, match="allowed_users\\[1\\] must be a string"):
            _parse_config(raw)

    def test_parse_security_rejects_empty_allowed_user(self):
        raw = {
            "security": {
                "allowed_users": ["WFAKE01", "   "],
            }
        }
        with pytest.raises(ValueError, match="allowed_users\\[1\\] cannot be empty"):
            _parse_config(raw)

    def test_parse_prompt_relay(self):
        raw = {
            "prompt_relay": {
                "enabled": True,
                "scan_lines": 20,
                "poll_every_cycles": 2,
                "option_patterns": [r"^\\d+\\.\\s*(.+)$"],
                "question_mark_required": False,
                "ttl_seconds": 120,
                "max_pending_per_issue": 2,
            }
        }
        config = _parse_config(raw)
        assert config.prompt_relay.enabled is True
        assert config.prompt_relay.scan_lines == 20
        assert config.prompt_relay.poll_every_cycles == 2
        assert config.prompt_relay.option_patterns == [r"^\\d+\\.\\s*(.+)$"]
        assert config.prompt_relay.question_mark_required is False
        assert config.prompt_relay.ttl_seconds == 120
        assert config.prompt_relay.max_pending_per_issue == 2

    def test_parse_prompt_relay_rejects_invalid_scan_lines(self):
        raw = {
            "prompt_relay": {
                "scan_lines": 0,
            }
        }
        with pytest.raises(ValueError, match="scan_lines must be > 0"):
            _parse_config(raw)

    def test_parse_prompt_relay_rejects_invalid_option_pattern_regex(self):
        raw = {
            "prompt_relay": {
                "option_patterns": ["("],
            }
        }
        with pytest.raises(ValueError, match="option_patterns\\[0\\] is not a valid regex"):
            _parse_config(raw)


class TestErrorPatterns:
    """Verify API error pattern configuration."""

    def test_default_error_patterns_exist(self):
        config = HomeboundConfig()
        assert len(config.sessions.error_patterns) > 0
        # Each pattern should be a valid regex
        for p in config.sessions.error_patterns:
            re.compile(p)

    def test_default_error_scan_lines(self):
        config = HomeboundConfig()
        assert config.sessions.error_scan_lines == 20

    def test_yaml_override_error_patterns(self):
        raw = {
            "sessions": {
                "error_patterns": [r"(?i)my_custom_error"],
                "error_scan_lines": 50,
            }
        }
        config = _parse_config(raw)
        assert config.sessions.error_patterns == [r"(?i)my_custom_error"]
        assert config.sessions.error_scan_lines == 50

    def test_invalid_error_pattern_raises(self):
        with pytest.raises(ValueError, match="error_patterns\\[0\\] is not a valid regex"):
            SessionsConfig(error_patterns=["("])
