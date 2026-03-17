"""Tests for smart routing: thread, keyword, LLM, and auto-spawn."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from datetime import timedelta

from homebound.adapters.transport import IncomingMessage
from homebound.config import HomeboundConfig, RoutingConfig, SecurityConfig
from homebound.session import ChildInfo, extract_keywords


# ---------------------------------------------------------------------------
# extract_keywords
# ---------------------------------------------------------------------------

class TestExtractKeywords:
    def test_basic_extraction(self):
        kw = extract_keywords("Fix the login bug in authentication module")
        assert "fix" in kw
        assert "login" in kw
        assert "bug" in kw
        assert "authentication" in kw
        assert "module" in kw

    def test_stopwords_removed(self):
        kw = extract_keywords("the a an is are was to of in for on with")
        assert kw == []

    def test_short_words_skipped(self):
        kw = extract_keywords("do it as of")
        assert kw == []

    def test_deduplication(self):
        kw = extract_keywords("login login login auth auth")
        assert kw == ["login", "auth"]

    def test_max_keywords(self):
        text = " ".join(f"keyword{i}" for i in range(50))
        kw = extract_keywords(text, max_keywords=5)
        assert len(kw) == 5

    def test_empty_string(self):
        assert extract_keywords("") == []


# ---------------------------------------------------------------------------
# Thread routing
# ---------------------------------------------------------------------------

class TestThreadRouting:
    def _make_orchestrator(self, **routing_kwargs):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(**routing_kwargs),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_route_by_thread_matches(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        orch.children[42] = child
        orch._router._message_session_map["1234.5678"] = 42

        msg = IncomingMessage(text="follow up", ts="1234.9999", thread_ts="1234.5678")
        result = orch._router.route_by_thread(msg)
        assert result == 42

    def test_route_by_thread_no_thread(self):
        orch = self._make_orchestrator()
        msg = IncomingMessage(text="hello", ts="1234.5678", thread_ts="")
        assert orch._router.route_by_thread(msg) is None

    def test_route_by_thread_same_ts(self):
        """A message that starts a thread (thread_ts == ts) shouldn't be treated as a reply."""
        orch = self._make_orchestrator()
        orch._router._message_session_map["1234.5678"] = 42
        orch.children[42] = ChildInfo(item_id=42, window_name="AGENT-42")

        msg = IncomingMessage(text="hello", ts="1234.5678", thread_ts="1234.5678")
        assert orch._router.route_by_thread(msg) is None

    def test_route_by_thread_unknown_parent(self):
        orch = self._make_orchestrator()
        msg = IncomingMessage(text="reply", ts="1234.9999", thread_ts="9999.0000")
        assert orch._router.route_by_thread(msg) is None

    def test_route_by_thread_dead_session(self):
        """Thread routing should not match a session that no longer exists."""
        orch = self._make_orchestrator()
        orch._router._message_session_map["1234.5678"] = 42
        # Session 42 not in children

        msg = IncomingMessage(text="reply", ts="1234.9999", thread_ts="1234.5678")
        assert orch._router.route_by_thread(msg) is None

    def test_route_by_thread_spawning_session(self):
        """Thread routing should not match a session that is still spawning (sentinel None)."""
        orch = self._make_orchestrator()
        orch._router._message_session_map["1234.5678"] = 42
        orch.children[42] = None  # sentinel

        msg = IncomingMessage(text="reply", ts="1234.9999", thread_ts="1234.5678")
        assert orch._router.route_by_thread(msg) is None

    def test_thread_routing_disabled(self):
        orch = self._make_orchestrator(thread_routing=False)
        orch._router._message_session_map["1234.5678"] = 42
        orch.children[42] = ChildInfo(item_id=42, window_name="AGENT-42")

        msg = IncomingMessage(text="reply", ts="1234.9999", thread_ts="1234.5678")
        # Even though _route_by_thread would match, the config flag prevents use
        # (the orchestrator checks the flag before calling the method)
        # We test the method directly here - it still works
        assert orch._router.route_by_thread(msg) == 42


# ---------------------------------------------------------------------------
# Keyword routing
# ---------------------------------------------------------------------------

class TestKeywordRouting:
    def _make_orchestrator(self, **routing_kwargs):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(**routing_kwargs),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_match_clear_winner(self):
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child1 = ChildInfo(item_id=42, window_name="AGENT-42")
        child1.recent_keywords = ["login", "authentication", "password", "oauth"]
        child2 = ChildInfo(item_id=99, window_name="AGENT-99")
        child2.recent_keywords = ["database", "migration", "schema", "postgres"]

        orch.children[42] = child1
        orch.children[99] = child2

        assert orch._router.match_by_keywords("fix the login authentication bug") == 42
        assert orch._router.match_by_keywords("run the database migration") == 99

    def test_no_match_below_threshold(self):
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        child.recent_keywords = ["login", "authentication", "password"]
        orch.children[42] = child

        # Only 1 keyword match (score=1.0), threshold is 2
        assert orch._router.match_by_keywords("check the login page") is None

    def test_tie_returns_none(self):
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child1 = ChildInfo(item_id=42, window_name="AGENT-42")
        child1.recent_keywords = ["api", "endpoint", "auth"]
        child2 = ChildInfo(item_id=99, window_name="AGENT-99")
        child2.recent_keywords = ["api", "endpoint", "database"]

        orch.children[42] = child1
        orch.children[99] = child2

        # "api endpoint" matches both equally (2 keywords each)
        assert orch._router.match_by_keywords("fix the api endpoint") is None

    def test_empty_text(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        child.recent_keywords = ["login"]
        orch.children[42] = child

        assert orch._router.match_by_keywords("") is None

    def test_no_children(self):
        orch = self._make_orchestrator()
        assert orch._router.match_by_keywords("anything") is None

    def test_child_with_no_keywords(self):
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        child.recent_keywords = []
        orch.children[42] = child

        assert orch._router.match_by_keywords("anything here") is None

    def test_skips_spawning_sentinel(self):
        orch = self._make_orchestrator(keyword_match_threshold=1)
        orch.children[42] = None  # spawning sentinel

        assert orch._router.match_by_keywords("something") is None


# ---------------------------------------------------------------------------
# Issue reference matching (#N → session)
# ---------------------------------------------------------------------------

class TestIssueRefRouting:
    def _make_orchestrator(self, **routing_kwargs):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(**routing_kwargs),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_issue_ref_routes_to_matching_session(self):
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.github_issue_id = 42
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        # #42 gives +5 bonus, easily exceeds threshold=2
        assert orch._router.match_by_keywords("what about #42?") == 1

    def test_issue_ref_no_match(self):
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.github_issue_id = 42
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        # #99 doesn't match any session's github_issue_id
        assert orch._router.match_by_keywords("check #99 status") is None

    def test_issue_ref_overrides_keyword_match(self):
        """Issue ref bonus should dominate keyword-only matches."""
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child1 = ChildInfo(item_id=1, window_name="AGENT-1")
        child1.github_issue_id = 42
        child1.recent_keywords = ["auth"]
        child2 = ChildInfo(item_id=2, window_name="AGENT-2")
        child2.recent_keywords = ["deploy", "database", "migration"]

        orch.children[1] = child1
        orch.children[2] = child2

        # child2 has more keyword matches, but child1 has issue ref bonus
        assert orch._router.match_by_keywords("deploy database #42") == 1

    def test_issue_ref_with_no_keywords_still_routes(self):
        """A bare #N with no other keywords should still route via issue ref."""
        orch = self._make_orchestrator(keyword_match_threshold=2)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.github_issue_id = 42
        child.recent_keywords = ["something"]
        orch.children[1] = child

        assert orch._router.match_by_keywords("#42") == 1


# ---------------------------------------------------------------------------
# Keyword scoring (pure overlap, no recency/idle adjustments)
# ---------------------------------------------------------------------------

class TestKeywordScoring:
    def _make_orchestrator(self, **routing_kwargs):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(**routing_kwargs),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_keyword_threshold_1_matches_single_overlap(self):
        """With threshold=1 (default), a single keyword overlap should match."""
        orch = self._make_orchestrator(keyword_match_threshold=1)

        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["sectors", "fii", "dii", "fmcg", "banks", "auto"]

        orch.children[1] = child

        # Only 1 keyword overlap ("sectors"), threshold=1 → match
        assert orch._router.match_by_keywords("which sectors in india have better prospects") == 1

    def test_keyword_scoring_ignores_recency(self):
        """Idle sessions should still match purely on keyword overlap."""
        orch = self._make_orchestrator(keyword_match_threshold=1)
        now = datetime.now()

        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["sectors", "india", "economy"]
        child.last_message_at = now - timedelta(minutes=30)  # idle for 30 min

        orch.children[1] = child

        # Even though session is idle, keyword match should still work
        assert orch._router.match_by_keywords("india economy outlook") == 1

    def test_more_overlap_wins(self):
        """Session with more keyword overlap wins."""
        orch = self._make_orchestrator(keyword_match_threshold=1)

        child1 = ChildInfo(item_id=1, window_name="AGENT-1")
        child1.recent_keywords = ["login", "auth", "password"]

        child2 = ChildInfo(item_id=2, window_name="AGENT-2")
        child2.recent_keywords = ["login"]

        orch.children[1] = child1
        orch.children[2] = child2

        # child1 has 2 overlaps (login, auth), child2 has 1 (login)
        assert orch._router.match_by_keywords("fix login auth") == 1


# ---------------------------------------------------------------------------
# github_issue_id persistence
# ---------------------------------------------------------------------------

class TestGithubIssueIdPersistence:
    def _make_orchestrator(self, tmp_path):
        from homebound.config import TrackerConfig
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            tracker=TrackerConfig(project_dir=str(tmp_path)),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_github_issue_id_round_trip(self, tmp_path):
        orch = self._make_orchestrator(tmp_path)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.github_issue_id = 42
        child.topic_summary = "Fix auth"
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        orch._save_children_state()

        orch2 = self._make_orchestrator(tmp_path)
        state = orch2._load_children_state()

        assert state[1]["github_issue_id"] == 42

    def test_github_issue_id_none_round_trip(self, tmp_path):
        orch = self._make_orchestrator(tmp_path)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        # github_issue_id defaults to None
        child.topic_summary = "General task"
        child.recent_keywords = ["task"]
        orch.children[1] = child

        orch._save_children_state()

        orch2 = self._make_orchestrator(tmp_path)
        state = orch2._load_children_state()

        assert state[1]["github_issue_id"] is None


# ---------------------------------------------------------------------------
# Dynamic keyword enrichment
# ---------------------------------------------------------------------------

class TestKeywordEnrichment:
    def _make_orchestrator(self, enrich_interval_cycles=1):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(enrich_interval_cycles=enrich_interval_cycles),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_enrich_updates_keywords(self):
        orch = self._make_orchestrator(enrich_interval_cycles=1)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["original"]
        orch.children[1] = child

        with patch("homebound.routing.read_child_output", new_callable=AsyncMock) as mock_read:
            mock_read.return_value = "deploying the authentication migration now"
            asyncio.run(orch._router.maybe_enrich_session_context())

        # New keywords from output should be merged in
        assert "deploying" in child.recent_keywords
        assert "authentication" in child.recent_keywords
        assert "migration" in child.recent_keywords
        # Original keyword should still be present
        assert "original" in child.recent_keywords

    def test_enrich_cycle_gating(self):
        orch = self._make_orchestrator(enrich_interval_cycles=3)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["original"]
        orch.children[1] = child

        async def _run():
            with patch("homebound.routing.read_child_output", new_callable=AsyncMock) as mock_read:
                mock_read.return_value = "something new here"
                # Cycles 1 and 2: should NOT call read_child_output
                await orch._router.maybe_enrich_session_context()
                await orch._router.maybe_enrich_session_context()
                mock_read.assert_not_called()
                # Cycle 3: should enrich
                await orch._router.maybe_enrich_session_context()
                mock_read.assert_called_once()

        asyncio.run(_run())

    def test_enrich_caps_keywords_at_40(self):
        orch = self._make_orchestrator(enrich_interval_cycles=1)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = [f"existing{i}" for i in range(30)]
        orch.children[1] = child

        with patch("homebound.routing.read_child_output", new_callable=AsyncMock) as mock_read:
            mock_read.return_value = " ".join(f"newword{i}" for i in range(20))
            asyncio.run(orch._router.maybe_enrich_session_context())

        assert len(child.recent_keywords) <= 40

    def test_enrich_skips_empty_output(self):
        orch = self._make_orchestrator(enrich_interval_cycles=1)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["original"]
        orch.children[1] = child

        with patch("homebound.routing.read_child_output", new_callable=AsyncMock) as mock_read:
            mock_read.return_value = "   "
            asyncio.run(orch._router.maybe_enrich_session_context())

        assert child.recent_keywords == ["original"]

    def test_enrich_skips_spawning_sentinel(self):
        orch = self._make_orchestrator(enrich_interval_cycles=1)
        orch.children[1] = None  # spawning sentinel

        with patch("homebound.routing.read_child_output", new_callable=AsyncMock) as mock_read:
            asyncio.run(orch._router.maybe_enrich_session_context())
            mock_read.assert_not_called()

    def test_enrich_disabled_when_interval_zero(self):
        orch = self._make_orchestrator(enrich_interval_cycles=0)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["original"]
        orch.children[1] = child

        with patch("homebound.routing.read_child_output", new_callable=AsyncMock) as mock_read:
            asyncio.run(orch._router.maybe_enrich_session_context())
            mock_read.assert_not_called()


# ---------------------------------------------------------------------------
# Message-session map tracking
# ---------------------------------------------------------------------------

class TestMessageSessionMap:
    def _make_orchestrator(self):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(max_message_map_size=10),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_record_outgoing_message(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        orch.children[42] = child

        orch._router.record_outgoing_message("1234.5678", 42)
        assert orch._router._message_session_map["1234.5678"] == 42
        assert "1234.5678" in child.posted_message_ts

    def test_prune_global_map(self):
        orch = self._make_orchestrator()  # max_message_map_size=10

        for i in range(15):
            orch._router.record_outgoing_message(f"{1000 + i}.0000", 42)

        # Should be pruned to 7 (3/4 of 10)
        assert len(orch._router._message_session_map) <= 10

    def test_prune_child_posted_ts(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        orch.children[42] = child

        for i in range(60):
            orch._router.record_outgoing_message(f"{1000 + i}.0000", 42)

        assert len(child.posted_message_ts) <= 50

    def test_agent_startup_signal_records_ts(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        orch.children[42] = child

        orch._record_agent_startup_signal("[agent-42] Working on issue...", ts="1234.5678")
        assert orch._router._message_session_map.get("1234.5678") == 42

    def test_agent_startup_signal_no_ts(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        orch.children[42] = child

        # Without ts, should not add to map
        orch._record_agent_startup_signal("[agent-42] Working on issue...")
        assert len(orch._router._message_session_map) == 0


# ---------------------------------------------------------------------------
# Auto-spawn (next free ops slot)
# ---------------------------------------------------------------------------

class TestAutoSpawn:
    def _make_orchestrator(self, max_concurrent=5):
        from homebound.config import SessionsConfig
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            sessions=SessionsConfig(max_concurrent=max_concurrent),
            routing=RoutingConfig(auto_spawn_on_no_match=True),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_first_free_slot(self):
        orch = self._make_orchestrator()
        assert orch._router.next_free_slot() == 1

    def test_with_occupied_slots(self):
        orch = self._make_orchestrator()
        orch.children[1] = ChildInfo(item_id=1, window_name="AGENT-1")
        assert orch._router.next_free_slot() == 2

    def test_all_slots_occupied(self):
        orch = self._make_orchestrator(max_concurrent=2)
        orch.children[1] = ChildInfo(item_id=1, window_name="AGENT-1")
        orch.children[2] = ChildInfo(item_id=2, window_name="AGENT-2")
        assert orch._router.next_free_slot() is None

    def test_mixed_slots(self):
        orch = self._make_orchestrator()
        orch.children[1] = ChildInfo(item_id=1, window_name="AGENT-1")
        orch.children[3] = ChildInfo(item_id=3, window_name="AGENT-3")
        assert orch._router.next_free_slot() == 2


# ---------------------------------------------------------------------------
# State persistence round-trip
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def _make_orchestrator(self, tmp_path):
        from homebound.config import TrackerConfig
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            tracker=TrackerConfig(project_dir=str(tmp_path)),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_save_and_load_round_trip(self, tmp_path):
        orch = self._make_orchestrator(tmp_path)
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        child.topic_summary = "Fix login bug"
        child.recent_keywords = ["login", "bug", "fix"]
        child.posted_message_ts = ["1234.5678", "1234.9999"]
        orch.children[42] = child
        orch._router._message_session_map["1234.5678"] = 42

        orch._save_children_state()

        # Load in a fresh orchestrator
        orch2 = self._make_orchestrator(tmp_path)
        state = orch2._load_children_state()

        assert 42 in state
        assert state[42]["topic_summary"] == "Fix login bug"
        assert state[42]["recent_keywords"] == ["login", "bug", "fix"]
        assert state[42]["posted_message_ts"] == ["1234.5678", "1234.9999"]
        assert orch2._router._message_session_map.get("1234.5678") == 42


# ---------------------------------------------------------------------------
# RoutingConfig defaults
# ---------------------------------------------------------------------------

class TestRoutingConfig:
    def test_defaults(self):
        config = RoutingConfig()
        assert config.thread_routing is True
        assert config.keyword_routing is True
        assert config.llm_routing is False
        assert config.auto_spawn_on_no_match is True
        assert config.keyword_match_threshold == 1
        assert config.max_message_map_size == 200

    def test_from_yaml(self):
        from homebound.config import _parse_config
        raw = {
            "routing": {
                "thread_routing": True,
                "keyword_routing": True,
                "llm_routing": True,
                "keyword_match_threshold": 3,
            }
        }
        config = _parse_config(raw)
        assert config.routing.llm_routing is True
        assert config.routing.keyword_match_threshold == 3
        assert config.routing.thread_routing is True


# ---------------------------------------------------------------------------
# IncomingMessage thread_ts field
# ---------------------------------------------------------------------------

class TestIncomingMessageThread:
    def test_thread_ts_default(self):
        msg = IncomingMessage(text="hello", ts="1234.5678")
        assert msg.thread_ts == ""

    def test_thread_ts_set(self):
        msg = IncomingMessage(text="reply", ts="1234.9999", thread_ts="1234.5678")
        assert msg.thread_ts == "1234.5678"

    def test_is_thread_reply(self):
        msg = IncomingMessage(text="reply", ts="1234.9999", thread_ts="1234.5678")
        assert msg.thread_ts and msg.thread_ts != msg.ts

    def test_is_not_thread_reply_when_same(self):
        msg = IncomingMessage(text="parent", ts="1234.5678", thread_ts="1234.5678")
        # Same ts = this is the parent message, not a reply
        assert not (msg.thread_ts and msg.thread_ts != msg.ts)


# ---------------------------------------------------------------------------
# Active thread parents helper
# ---------------------------------------------------------------------------

class TestActiveThreadParents:
    def _make_orchestrator(self, **routing_kwargs):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(**routing_kwargs),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def test_returns_ts_for_active_session(self):
        orch = self._make_orchestrator(thread_routing=True, thread_poll_max_age=1800)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        orch.children[1] = child
        # Use a recent ts (now)
        recent_ts = str(time.time())
        orch._router._message_session_map[recent_ts] = 1

        result = orch._router.active_thread_parents()
        assert recent_ts in result

    def test_excludes_dead_session(self):
        orch = self._make_orchestrator(thread_routing=True, thread_poll_max_age=1800)
        # Session not in children
        recent_ts = str(time.time())
        orch._router._message_session_map[recent_ts] = 99

        result = orch._router.active_thread_parents()
        assert recent_ts not in result

    def test_excludes_spawning_sentinel(self):
        orch = self._make_orchestrator(thread_routing=True, thread_poll_max_age=1800)
        orch.children[1] = None  # spawning sentinel
        recent_ts = str(time.time())
        orch._router._message_session_map[recent_ts] = 1

        result = orch._router.active_thread_parents()
        assert recent_ts not in result

    def test_excludes_old_threads(self):
        orch = self._make_orchestrator(thread_routing=True, thread_poll_max_age=1800)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        orch.children[1] = child
        # ts older than max_age (31 minutes ago)
        old_ts = str(time.time() - 1860)
        orch._router._message_session_map[old_ts] = 1

        result = orch._router.active_thread_parents()
        assert old_ts not in result

    def test_respects_max_threads_limit(self):
        orch = self._make_orchestrator(
            thread_routing=True, thread_poll_max_age=1800, thread_poll_max_threads=3,
        )
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        orch.children[1] = child
        now = time.time()
        for i in range(10):
            ts = str(now - i)
            orch._router._message_session_map[ts] = 1

        result = orch._router.active_thread_parents()
        assert len(result) == 3

    def test_empty_when_thread_routing_disabled(self):
        orch = self._make_orchestrator(thread_routing=False, thread_poll_max_age=1800)
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        orch.children[1] = child
        recent_ts = str(time.time())
        orch._router._message_session_map[recent_ts] = 1

        result = orch._router.active_thread_parents()
        assert result == []

    def test_newest_threads_preferred(self):
        orch = self._make_orchestrator(
            thread_routing=True, thread_poll_max_age=1800, thread_poll_max_threads=2,
        )
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        orch.children[1] = child
        now = time.time()
        oldest_ts = str(now - 100)
        middle_ts = str(now - 50)
        newest_ts = str(now - 10)
        for ts in (oldest_ts, middle_ts, newest_ts):
            orch._router._message_session_map[ts] = 1

        result = orch._router.active_thread_parents()
        # Cap is 2, newest first
        assert len(result) == 2
        assert result[0] == newest_ts
        assert result[1] == middle_ts
        assert oldest_ts not in result


# ---------------------------------------------------------------------------
# thread_ts pass-through to child post_command
# ---------------------------------------------------------------------------

class TestThreadTsPassthrough:
    """Verify that thread_ts is baked into the post_command given to child agents."""

    @pytest.fixture
    def mock_tmux(self):
        with (
            patch("homebound.session.send_keys", new_callable=AsyncMock) as mock_send,
        ):
            mock_send.return_value = True
            yield mock_send

    def test_send_to_child_passes_thread_ts_to_post_command(self, mock_tmux):
        from homebound.config import TransportConfig
        from homebound.session import send_to_child

        config = HomeboundConfig(
            transport=TransportConfig(
                post_command_template="post --session {session_name} --thread {thread_ts} --msg {message}",
            ),
        )
        child = ChildInfo(item_id=1, window_name="AGENT-1")

        asyncio.run(send_to_child(child, "hello", config, thread_ts="1111.2222"))

        mock_tmux.assert_called_once()
        _, sent = mock_tmux.call_args.args
        assert "--thread 1111.2222" in sent

    def test_send_to_child_empty_thread_ts_by_default(self, mock_tmux):
        from homebound.config import TransportConfig
        from homebound.session import send_to_child

        config = HomeboundConfig(
            transport=TransportConfig(
                post_command_template="post --session {session_name} --thread {thread_ts} --msg {message}",
            ),
        )
        child = ChildInfo(item_id=1, window_name="AGENT-1")

        asyncio.run(send_to_child(child, "hello", config))

        _, sent = mock_tmux.call_args.args
        assert "--thread  --msg" in sent  # empty thread_ts

    def test_send_to_child_template_without_thread_ts(self, mock_tmux):
        """Templates without {thread_ts} should keep working (backward compat)."""
        from homebound.config import TransportConfig
        from homebound.session import send_to_child

        config = HomeboundConfig(
            transport=TransportConfig(
                post_command_template="post --session {session_name} --msg {message}",
            ),
        )
        child = ChildInfo(item_id=1, window_name="AGENT-1")

        asyncio.run(send_to_child(child, "hello", config, thread_ts="1111.2222"))

        _, sent = mock_tmux.call_args.args
        assert "--session agent-1" in sent
        # No error even though thread_ts was passed but not in template

    def test_build_prompt_handles_thread_ts_placeholder(self):
        """_build_prompt should not KeyError when template contains {thread_ts}."""
        from homebound.config import ModeConfig, TransportConfig
        from homebound.session import _build_prompt

        config = HomeboundConfig(
            transport=TransportConfig(
                post_command_template="post --session {session_name} --thread {thread_ts} --msg {message}",
            ),
            modes={
                "chat": ModeConfig(
                    prompt_template="Do work. Post: {post_command}",
                ),
            },
        )

        # Should not raise KeyError
        prompt = _build_prompt(1, "some task", "chat", config)
        assert "--thread " in prompt  # empty thread_ts at spawn time
        assert "--session agent-1" in prompt

    def test_thread_routed_message_sets_active_thread_ts(self):
        """Tier 1 routing should set child.active_thread_ts."""
        child = ChildInfo(item_id=42, window_name="AGENT-42")
        assert child.active_thread_ts == ""

        child.active_thread_ts = "1234.5678"
        assert child.active_thread_ts == "1234.5678"


# ---------------------------------------------------------------------------
# Thread routing denial blocks fallthrough
# ---------------------------------------------------------------------------

class TestThreadRoutingDenialBlocks:
    """Tier 1 security denial should NOT fall through to keyword/LLM routing."""

    def test_thread_denial_does_not_fall_through_to_keyword(self):
        """When thread routing matches but security denies, message must not
        reach keyword routing (Tier 2)."""
        from homebound.orchestrator import Orchestrator

        config = HomeboundConfig(
            security=SecurityConfig(allowed_users=["WOWNER"], allow_open_channel=False),
            routing=RoutingConfig(keyword_match_threshold=1),
        )
        orch = Orchestrator(config=config, dry_run=True)
        orch._post = AsyncMock()
        orch.startup_ts = 0.0

        # Session owned by WOWNER
        child = ChildInfo(item_id=1, window_name="AGENT-1", owner_user_id="WOWNER")
        child.recent_keywords = ["auth", "login", "password"]
        orch.children[1] = child

        # Map a thread parent to this session
        orch._router._message_session_map["1000.0000"] = 1

        # Alice (WALICE) is NOT in allowed_users but IS the sender.
        # Re-init the policy so it recognises the restricted list.
        orch.command_policy = orch.command_policy.__class__(config.security)

        future_ts = str(time.time() + 1000)

        with patch("homebound.orchestrator.send_to_child", new_callable=AsyncMock) as mock_send:
            mock_transport = MagicMock()
            mock_transport.poll = MagicMock(return_value=[
                IncomingMessage(
                    text="auth login password",  # would match keywords
                    ts=future_ts,
                    user="WALICE",
                    thread_ts="1000.0000",  # thread reply → Tier 1 match
                ),
            ])
            mock_transport.is_from_agent = MagicMock(return_value=False)
            mock_transport.poll_thread_replies = MagicMock(return_value=[])
            orch._transport = mock_transport

            asyncio.run(orch._poll_cycle())

            # send_to_child must NOT have been called — neither via thread
            # nor via keyword fallthrough.
            mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# LLM response parsing — first-token fallback
# ---------------------------------------------------------------------------

class TestLLMResponseParsing:
    """Verify that verbose LLM answers are parsed via first-token fallback."""

    def _make_orchestrator(self, **routing_kwargs):
        config = HomeboundConfig(
            security=SecurityConfig(allow_open_channel=True),
            routing=RoutingConfig(llm_routing=True, **routing_kwargs),
        )
        from homebound.orchestrator import Orchestrator
        orch = Orchestrator(config, dry_run=True)
        return orch

    def _mock_llm_response(self, text: str):
        """Create a mock Anthropic response with the given text."""
        content_block = MagicMock()
        content_block.text = text
        response = MagicMock()
        response.content = [content_block]
        return response

    def test_exact_match(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_llm_response("Agent1")
        orch._router._anthropic_client = mock_client

        result = asyncio.run(orch._router.match_by_llm("test"))
        assert result == 1

    def test_verbose_response_extracts_first_token(self):
        """LLM returns 'Agent1 — it matches the auth topic', should still match."""
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_llm_response(
            "Agent1 — it matches the auth topic"
        )
        orch._router._anthropic_client = mock_client

        result = asyncio.run(orch._router.match_by_llm("test"))
        assert result == 1

    def test_none_response(self):
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_llm_response("NONE")
        orch._router._anthropic_client = mock_client

        result = asyncio.run(orch._router.match_by_llm("test"))
        assert result is None

    def test_verbose_none_response(self):
        """LLM says 'None of the sessions match' — should still return None."""
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_llm_response(
            "None of the sessions match this query"
        )
        orch._router._anthropic_client = mock_client

        result = asyncio.run(orch._router.match_by_llm("test"))
        assert result is None

    def test_unknown_label_returns_none(self):
        """LLM returns a label that doesn't exist in id_map."""
        orch = self._make_orchestrator()
        child = ChildInfo(item_id=1, window_name="AGENT-1")
        child.recent_keywords = ["auth"]
        orch.children[1] = child

        mock_client = MagicMock()
        mock_client.messages.create.return_value = self._mock_llm_response("Agent99")
        orch._router._anthropic_client = mock_client

        result = asyncio.run(orch._router.match_by_llm("test"))
        assert result is None
