"""Tests for multi-runtime (multi-model) agent pools.

Covers config parsing, session naming, command routing, state persistence,
and orphan adoption for the multi-model feature (gh#44).
"""

from __future__ import annotations

import pytest

from homebound.config import HomeboundConfig, RuntimeConfig, _parse_config
from homebound.session import (
    ChildInfo,
    _item_label,
    parse_window_name,
    session_name,
    window_name,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestMultiRuntimeConfig:
    """Config parsing for runtimes: (plural) YAML key."""

    def test_empty_runtimes_is_single_runtime(self):
        cfg = HomeboundConfig()
        assert cfg.runtimes == {}
        assert not cfg.is_multi_runtime

    def test_parse_multi_runtime(self):
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code", "command": "claude --skip"},
                "codex": {"type": "generic", "command": "codex --full-auto"},
            },
        })
        assert len(cfg.runtimes) == 2
        assert cfg.is_multi_runtime
        assert cfg.runtimes["claude"].type == "claude-code"
        assert cfg.runtimes["codex"].command == "codex --full-auto"

    def test_pool_names_sorted(self):
        cfg = _parse_config({
            "runtimes": {
                "codex": {"type": "generic"},
                "claude": {"type": "claude-code"},
            },
        })
        assert cfg.pool_names == ["claude", "codex"]

    def test_default_pool_is_first_alpha(self):
        cfg = _parse_config({
            "runtimes": {
                "codex": {"type": "generic"},
                "claude": {"type": "claude-code"},
            },
        })
        assert cfg.default_pool == "claude"

    def test_pool_label(self):
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code"},
                "codex": {"type": "generic"},
            },
        })
        assert cfg.pool_label("claude") == "Claude"
        assert cfg.pool_label("codex") == "Codex"

    def test_pool_window_prefix(self):
        cfg = _parse_config({
            "runtimes": {"claude": {"type": "claude-code"}},
        })
        assert cfg.pool_window_prefix("claude") == "CLAUDE-"

    def test_pool_session_prefix(self):
        cfg = _parse_config({
            "runtimes": {"claude": {"type": "claude-code"}},
        })
        assert cfg.pool_session_prefix("claude") == "claude-"

    def test_non_alpha_pool_name_rejected(self):
        with pytest.raises(ValueError, match="alphabetic"):
            _parse_config({"runtimes": {"my-pool": {"type": "generic"}}})

    def test_pool_names_lowercased(self):
        cfg = _parse_config({
            "runtimes": {"Claude": {"type": "claude-code"}},
        })
        assert "claude" in cfg.runtimes

    def test_backward_compat_single_runtime(self):
        """When only runtime: (singular) is present, behavior is unchanged."""
        cfg = _parse_config({"runtime": {"type": "claude-code", "command": "claude"}})
        assert cfg.runtimes == {}
        assert not cfg.is_multi_runtime
        assert cfg.default_pool == "agent"  # derived from agent_label
        rt = cfg.get_runtime()
        assert rt.command == "claude"

    def test_get_runtime_for_pool(self):
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code", "command": "claude --fast"},
                "codex": {"type": "generic", "command": "codex --auto"},
            },
        })
        rt_claude = cfg.get_runtime_for_pool("claude")
        assert rt_claude.command == "claude --fast"
        rt_codex = cfg.get_runtime_for_pool("codex")
        assert rt_codex.command == "codex --auto"

    def test_get_runtime_for_pool_caches(self):
        cfg = _parse_config({
            "runtimes": {"claude": {"type": "claude-code"}},
        })
        rt1 = cfg.get_runtime_for_pool("claude")
        rt2 = cfg.get_runtime_for_pool("claude")
        assert rt1 is rt2

    def test_get_runtime_for_unknown_pool_raises(self):
        cfg = _parse_config({
            "runtimes": {"claude": {"type": "claude-code"}},
        })
        with pytest.raises(ValueError, match="Unknown runtime pool"):
            cfg.get_runtime_for_pool("nonexistent")

    def test_get_runtime_with_pool_name_kwarg(self):
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code", "command": "claude"},
                "codex": {"type": "generic", "command": "codex"},
            },
        })
        rt = cfg.get_runtime(pool_name="codex")
        assert rt.command == "codex"

    def test_ignored_prefixes_include_pool_labels(self):
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code"},
                "codex": {"type": "generic"},
            },
        })
        prefixes = cfg.ignored_prefixes
        assert "Claude" in prefixes
        assert "Codex" in prefixes


# ---------------------------------------------------------------------------
# Session naming
# ---------------------------------------------------------------------------


class TestPoolAwareNaming:
    """Window/session/label naming with pool_name."""

    @pytest.fixture
    def multi_cfg(self):
        return _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code"},
                "codex": {"type": "generic"},
            },
        })

    @pytest.fixture
    def single_cfg(self):
        return _parse_config({"runtime": {"type": "claude-code"}})

    def test_window_name_multi(self, multi_cfg):
        assert window_name(multi_cfg, 1, "claude") == "CLAUDE-1"
        assert window_name(multi_cfg, 3, "codex") == "CODEX-3"

    def test_window_name_single(self, single_cfg):
        assert window_name(single_cfg, 1) == "AGENT-1"

    def test_session_name_multi(self, multi_cfg):
        assert session_name(multi_cfg, 1, "claude") == "claude-1"
        assert session_name(multi_cfg, 2, "codex") == "codex-2"

    def test_session_name_single(self, single_cfg):
        assert session_name(single_cfg, 1) == "agent-1"

    def test_item_label_multi(self, multi_cfg):
        assert _item_label(multi_cfg, 1, "claude") == "Claude1"
        assert _item_label(multi_cfg, 2, "codex") == "Codex2"

    def test_item_label_single(self, single_cfg):
        assert _item_label(single_cfg, 1) == "Agent1"

    def test_parse_window_name_multi(self, multi_cfg):
        slot, pool = parse_window_name("CLAUDE-1", multi_cfg)
        assert slot == 1 and pool == "claude"
        slot, pool = parse_window_name("CODEX-3", multi_cfg)
        assert slot == 3 and pool == "codex"

    def test_parse_window_name_unknown(self, multi_cfg):
        slot, pool = parse_window_name("UNKNOWN-1", multi_cfg)
        assert slot is None

    def test_parse_window_name_single(self, single_cfg):
        slot, pool = parse_window_name("AGENT-1", single_cfg)
        assert slot == 1 and pool == ""


# ---------------------------------------------------------------------------
# ChildInfo
# ---------------------------------------------------------------------------


class TestChildInfoPoolName:
    """ChildInfo pool_name field."""

    def test_default_pool_name_empty(self):
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        assert child.pool_name == ""

    def test_pool_name_set(self):
        child = ChildInfo(item_id=1, window_name="CLAUDE-1", pool_name="claude")
        assert child.pool_name == "claude"


# ---------------------------------------------------------------------------
# Orchestrator command parsing
# ---------------------------------------------------------------------------


class TestMultiPoolCommandParsing:
    """_parse_role_command with multi-pool configs."""

    @pytest.fixture
    def multi_orch(self):
        from homebound.orchestrator import Orchestrator
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code"},
                "codex": {"type": "generic"},
            },
            "security": {"allow_open_channel": True},
        })
        return Orchestrator(config=cfg, dry_run=True)

    @pytest.fixture
    def single_orch(self):
        from homebound.orchestrator import Orchestrator
        cfg = _parse_config({
            "security": {"allow_open_channel": True},
        })
        return Orchestrator(config=cfg, dry_run=True)

    def test_parse_claude_with_slot(self, multi_orch):
        result = multi_orch._parse_role_command("@Claude1 fix the bug")
        assert result is not None
        pool, slot, payload = result
        assert pool == "claude"
        assert slot == 1
        assert payload == "fix the bug"

    def test_parse_codex_with_slot(self, multi_orch):
        result = multi_orch._parse_role_command("@Codex2 refactor utils")
        assert result is not None
        pool, slot, payload = result
        assert pool == "codex"
        assert slot == 2
        assert payload == "refactor utils"

    def test_parse_pool_without_slot(self, multi_orch):
        result = multi_orch._parse_role_command("@Claude do something")
        assert result is not None
        pool, slot, payload = result
        assert pool == "claude"
        assert slot is None
        assert payload == "do something"

    def test_parse_agent_label_still_works(self, multi_orch):
        result = multi_orch._parse_role_command("@Agent1 fix it")
        assert result is not None
        pool, slot, payload = result
        assert pool == ""  # agent_label, not a pool
        assert slot == 1

    def test_parse_single_runtime_backward_compat(self, single_orch):
        result = single_orch._parse_role_command("@Agent1 do task")
        assert result is not None
        pool, slot, payload = result
        assert pool == ""
        assert slot == 1
        assert payload == "do task"

    def test_parse_unknown_prefix_returns_none(self, multi_orch):
        result = multi_orch._parse_role_command("@Gemini1 hello")
        assert result is None

    def test_parse_case_insensitive(self, multi_orch):
        result = multi_orch._parse_role_command("@CLAUDE1 fix it")
        assert result is not None
        pool, slot, payload = result
        assert pool == "claude"

    def test_resolve_label_multi(self, multi_orch):
        item_id, pool = multi_orch._resolve_label_to_item_id("Claude1")
        assert item_id == 1 and pool == "claude"

    def test_resolve_label_codex(self, multi_orch):
        item_id, pool = multi_orch._resolve_label_to_item_id("Codex3")
        assert item_id == 3 and pool == "codex"

    def test_resolve_label_raw_int(self, multi_orch):
        item_id, pool = multi_orch._resolve_label_to_item_id("5")
        assert item_id == 5 and pool == ""


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


class TestMultiRuntimeStatePersistence:
    """Verify pool_name is saved/loaded in children.json."""

    def test_state_includes_pool_name(self):
        from homebound.orchestrator import Orchestrator
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code"},
                "codex": {"type": "generic"},
            },
            "security": {"allow_open_channel": True},
        })
        orch = Orchestrator(config=cfg, dry_run=True)
        child = ChildInfo(
            item_id=1, window_name="CLAUDE-1",
            pool_name="claude", topic_summary="test",
        )
        orch.children[1] = child
        orch._save_children_state()

        # Load state back
        state = orch._load_children_state()
        assert state[1]["pool_name"] == "claude"

    def test_state_missing_pool_name_defaults_empty(self):
        from homebound.orchestrator import Orchestrator
        cfg = _parse_config({
            "security": {"allow_open_channel": True},
        })
        orch = Orchestrator(config=cfg, dry_run=True)

        # Simulate old-format state (no pool_name)
        import json
        state_data = {
            "children": {
                "1": {
                    "window_name": "AGENT-1",
                    "started_at": "2024-01-01T00:00:00",
                    "last_message_at": "2024-01-01T00:00:00",
                    "owner_user_id": "",
                    "topic_summary": "old task",
                    "recent_keywords": [],
                    "posted_message_ts": [],
                    "github_issue_id": None,
                },
            },
            "message_session_map": {},
        }
        orch._state_file.parent.mkdir(parents=True, exist_ok=True)
        orch._state_file.write_text(json.dumps(state_data))

        state = orch._load_children_state()
        assert state[1]["pool_name"] == ""


# ---------------------------------------------------------------------------
# Regression tests for code review findings
# ---------------------------------------------------------------------------


class TestReviewRegressions:
    """Regression tests for issues found during code review."""

    def test_is_multi_runtime_true_for_single_pool(self):
        """Issue 1: is_multi_runtime must be True even with one pool."""
        cfg = _parse_config({
            "runtimes": {"claude": {"type": "claude-code"}},
        })
        assert cfg.is_multi_runtime
        assert cfg.pool_label("claude") == "Claude"

    def test_is_multi_runtime_false_without_runtimes(self):
        cfg = _parse_config({"runtime": {"type": "claude-code"}})
        assert not cfg.is_multi_runtime

    def test_health_check_uses_per_pool_markers(self):
        """Issue 2: health check must use per-pool idle markers, not global."""
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code", "idle_markers": ["\u276f"]},
                "codex": {"type": "generic", "idle_markers": ["$"]},
            },
        })
        rt_claude = cfg.get_runtime_for_pool("claude")
        rt_codex = cfg.get_runtime_for_pool("codex")
        assert rt_claude.idle_prompt_markers() == ["\u276f"]
        assert rt_codex.idle_prompt_markers() == ["$"]
        # Verify get_runtime(pool_name) dispatches correctly
        assert cfg.get_runtime("codex").idle_prompt_markers() == ["$"]

    def test_pool_command_no_slot_spawns_correct_pool(self):
        """Issue 3: @Claude <task> (no slot) must spawn on claude pool."""
        from homebound.orchestrator import Orchestrator
        cfg = _parse_config({
            "runtimes": {
                "claude": {"type": "claude-code"},
                "codex": {"type": "generic"},
            },
            "security": {"allow_open_channel": True},
        })
        orch = Orchestrator(config=cfg, dry_run=True)
        # @Claude without slot — parse should return pool but no slot
        result = orch._parse_role_command("@Claude fix this bug")
        assert result is not None
        pool, slot, payload = result
        assert pool == "claude"
        assert slot is None
        assert payload == "fix this bug"

    def test_orphan_adoption_prefers_window_inferred_pool(self):
        """Issue 4: window-inferred pool_name must not be overwritten by empty saved state."""
        import asyncio
        import json
        from unittest.mock import AsyncMock, patch
        from homebound.orchestrator import Orchestrator
        cfg = _parse_config({
            "runtimes": {"claude": {"type": "claude-code"}},
            "security": {"allow_open_channel": True},
        })
        orch = Orchestrator(config=cfg, dry_run=True)

        # Simulate saved state with empty pool_name (legacy migration)
        state_data = {
            "children": {
                "1": {
                    "window_name": "CLAUDE-1",
                    "started_at": "2024-01-01T00:00:00",
                    "last_message_at": "2024-01-01T00:00:00",
                    "owner_user_id": "",
                    "topic_summary": "test",
                    "recent_keywords": [],
                    "posted_message_ts": [],
                    "github_issue_id": None,
                    "pool_name": "",
                },
            },
            "message_session_map": {},
        }
        orch._state_file.parent.mkdir(parents=True, exist_ok=True)
        orch._state_file.write_text(json.dumps(state_data))

        # Verify parse_window_name correctly infers pool from CLAUDE-1
        slot, pool = parse_window_name("CLAUDE-1", cfg)
        assert slot == 1
        assert pool == "claude"  # Must be "claude", not ""

        # Exercise the actual _adopt_orphans path with mocked tmux
        with patch("homebound.orchestrator.tmux_list_windows", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = ["CLAUDE-1"]
            with patch("homebound.session.list_windows", new_callable=AsyncMock) as mock_list2:
                mock_list2.return_value = ["CLAUDE-1"]
                adopted = asyncio.run(orch._adopt_orphans())
        assert len(adopted) == 1
        child = orch.children[1]
        assert child is not None
        assert child.pool_name == "claude"  # Must be inferred, not ""
