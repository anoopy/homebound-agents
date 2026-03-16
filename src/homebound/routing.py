"""Routing engine — thread, keyword, and LLM-based message routing.

Extracted from orchestrator.py to keep routing logic self-contained.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime

from homebound.config import HomeboundConfig
from homebound.session import ChildInfo, extract_keywords, read_child_output

logger = logging.getLogger("homebound")


class RoutingEngine:
    """Stateful message router: thread lookup, keyword scoring, LLM matching.

    Parameters
    ----------
    config:
        Full homebound config (routing, sessions, prompt_relay sub-configs used).
    children:
        Reference to orchestrator's children dict (shared, mutated externally).
    pending_prompts_fn:
        Callable returning active PendingPrompt list for an item_id
        (reserved for future LLM routing context).
    save_state_fn:
        Callable invoked after keyword enrichment to persist state.
    """

    def __init__(
        self,
        config: HomeboundConfig,
        children: dict[int, ChildInfo | None],
        pending_prompts_fn: Callable,
        save_state_fn: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.children = children
        self._pending_prompts_fn = pending_prompts_fn
        self._save_state_fn = save_state_fn

        # Smart routing: maps Slack message ts -> item_id for thread routing
        self._message_session_map: dict[str, int] = {}
        self._enrich_cycle_counter: int = 0
        self._anthropic_client = None  # Lazy-init, reused across LLM routing calls
        # Recent channel messages for LLM routing context
        self._recent_messages: list[tuple[str, str]] = []  # (sender_label, text)
        self._max_recent_messages: int = 20

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def message_session_map(self) -> dict[str, int]:
        return self._message_session_map

    @message_session_map.setter
    def message_session_map(self, value: dict[str, int]) -> None:
        self._message_session_map = value

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _item_label(item_id: int) -> str:
        return f"Claude{item_id}"

    # ------------------------------------------------------------------
    # Thread routing
    # ------------------------------------------------------------------

    def route_by_thread(self, msg) -> int | None:
        """Route a thread reply to the session that posted the parent message."""
        if not msg.thread_ts or msg.thread_ts == msg.ts:
            return None
        item_id = self._message_session_map.get(msg.thread_ts)
        if item_id is not None and item_id in self.children and self.children[item_id] is not None:
            return item_id
        return None

    def active_thread_parents(self) -> list[str]:
        """Return thread parent ts values to poll for replies.

        Returns ts values from _message_session_map that:
        - Have an active (non-None) session in children
        - Are younger than thread_poll_max_age seconds
        - Sorted newest-first, capped at thread_poll_max_threads
        - Empty list when thread_routing is disabled
        """
        if not self.config.routing.thread_routing:
            return []

        max_age = self.config.routing.thread_poll_max_age
        max_threads = self.config.routing.thread_poll_max_threads
        now = time.time()
        cutoff = now - max_age

        candidates: list[str] = []
        for ts, item_id in self._message_session_map.items():
            # Skip sessions that are gone or still spawning
            if item_id not in self.children or self.children[item_id] is None:
                continue
            # Skip threads older than max_age
            try:
                if float(ts) < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            candidates.append(ts)

        # Newest-first (largest ts value first), cap at max_threads
        candidates.sort(reverse=True)
        return candidates[:max_threads]

    # ------------------------------------------------------------------
    # Keyword routing
    # ------------------------------------------------------------------

    def match_by_keywords(self, text: str) -> int | None:
        """Match incoming text against active session keywords.

        Scoring: keyword overlap + issue ref bonus + recency bonus - idle penalty.
        Returns the item_id of the best-matching session, or None if no
        clear match (below threshold or ambiguous tie).
        """
        threshold = self.config.routing.keyword_match_threshold
        incoming_words = set(extract_keywords(text))

        # Check for issue reference (#N) in incoming text
        issue_ref_match = re.search(r"#(\d+)\b", text)
        issue_ref_id = int(issue_ref_match.group(1)) if issue_ref_match else None

        # Allow routing even with no keywords if we have an issue ref
        if not incoming_words and issue_ref_id is None:
            return None

        best_id: int | None = None
        best_score: float = 0.0
        tied = False
        now = datetime.now()

        for item_id, child in self.children.items():
            if child is None:
                continue

            # Base score: keyword overlap
            child_keywords = set(child.recent_keywords)
            score: float = len(incoming_words & child_keywords) if child_keywords else 0.0

            # Issue reference bonus: strong signal when #N matches github_issue_id
            if issue_ref_id is not None and child.github_issue_id == issue_ref_id:
                score += 5.0

            if score == 0.0:
                continue

            # Recency bonus: sessions active in last 5 min get +1
            age_seconds = (now - child.last_message_at).total_seconds()
            if age_seconds < 300:
                score += 1.0

            # Idle penalty: sessions idle > 15 min get -0.5
            if age_seconds > 900:
                score -= 0.5

            if score > best_score + 0.01:
                best_score = score
                best_id = item_id
                tied = False
            elif abs(score - best_score) < 0.01 and score > 0:
                tied = True

        if best_score >= threshold and not tied:
            return best_id
        return None

    # ------------------------------------------------------------------
    # LLM routing
    # ------------------------------------------------------------------

    async def match_by_llm(self, text: str) -> int | None:
        """Use an LLM to match incoming text to the best session.

        Provides recent conversation context and session metadata so the
        LLM can distinguish follow-ups from new/unrelated messages.

        Returns item_id or None if the LLM says no match.
        """
        if not self.children:
            return None

        # Build session descriptions with keywords for richer context
        session_lines: list[str] = []
        id_map: dict[str, int] = {}
        for item_id, child in self.children.items():
            if child is None:
                continue
            label = self._item_label(item_id)
            summary = child.topic_summary or "(no summary)"
            kw_str = ", ".join(child.recent_keywords[:10]) if child.recent_keywords else ""
            desc = f"- {label}: {summary}"
            if kw_str:
                desc += f" (keywords: {kw_str})"
            session_lines.append(desc)
            id_map[label.lower()] = item_id

        if not session_lines:
            return None

        # Build recent conversation context
        context_lines: list[str] = []
        for sender_label, msg_text in self._recent_messages[-5:]:
            context_lines.append(f"[{sender_label}] {msg_text}")
        context_block = "\n".join(context_lines) if context_lines else "(no recent messages)"

        prompt = (
            "You are a strict message router. Given the recent conversation and active sessions, "
            "decide if the NEW message is a follow-up to an existing session.\n\n"
            "Rules:\n"
            "- Respond with ONLY the session label (e.g. Claude1) if the new message clearly continues that session's topic\n"
            "- Respond NONE if it is a new or unrelated topic\n"
            "- Respond NONE if unsure\n"
            "- Default to NONE — only match when confident\n\n"
            f"Active sessions:\n" + "\n".join(session_lines) + "\n\n"
            f"Recent conversation:\n{context_block}\n\n"
            f"NEW message to route: {text[:500]}\n\n"
            "Session label or NONE:"
        )

        try:
            if self._anthropic_client is None:
                import anthropic
                self._anthropic_client = anthropic.Anthropic()
            response = self._anthropic_client.messages.create(
                model=self.config.routing.llm_model,
                max_tokens=50,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = response.content[0].text.strip().lower()
            answer = answer.strip(".*:- ")
            if re.search(r"\bnone\b", answer):
                return None
            return id_map.get(answer)
        except Exception as e:
            logger.warning("LLM routing failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Slot management
    # ------------------------------------------------------------------

    def next_free_slot(self) -> int | None:
        """Find the next available slot number, or None if at capacity."""
        max_children = self.config.sessions.max_concurrent
        for slot in range(1, max_children + 1):
            if slot not in self.children:
                return slot
        return None

    # ------------------------------------------------------------------
    # Message tracking
    # ------------------------------------------------------------------

    def record_recent_message(self, sender_label: str, text: str) -> None:
        """Append to rolling message buffer for LLM routing context."""
        truncated = text[:300] if len(text) > 300 else text
        self._recent_messages.append((sender_label, truncated))
        if len(self._recent_messages) > self._max_recent_messages:
            self._recent_messages = self._recent_messages[-self._max_recent_messages:]

    def record_outgoing_message(self, ts: str, item_id: int) -> None:
        """Record a Slack message ts -> item_id mapping for thread routing."""
        max_size = self.config.routing.max_message_map_size
        self._message_session_map[ts] = item_id
        # Also track on the child itself
        child = self.children.get(item_id)
        if child is not None:
            child.posted_message_ts.append(ts)
            if len(child.posted_message_ts) > 50:
                child.posted_message_ts = child.posted_message_ts[-50:]
        # Prune global map
        if len(self._message_session_map) > max_size:
            to_keep = max_size * 3 // 4
            all_ts = sorted(self._message_session_map.keys())
            for old_ts in all_ts[:-to_keep]:
                del self._message_session_map[old_ts]

    # ------------------------------------------------------------------
    # Session context enrichment
    # ------------------------------------------------------------------

    async def maybe_enrich_session_context(self) -> None:
        """Periodically refresh keywords from child tmux output for better routing."""
        interval = self.config.routing.enrich_interval_cycles
        if interval <= 0:
            return
        self._enrich_cycle_counter += 1
        if self._enrich_cycle_counter < interval:
            return
        self._enrich_cycle_counter = 0

        if not self.children:
            return

        enriched_any = False
        for item_id, child in self.children.items():
            if child is None:
                continue
            try:
                output = await read_child_output(
                    child, self.config,
                    lines=self.config.prompt_relay.scan_lines,
                )
                if not output.strip():
                    continue
                new_keywords = extract_keywords(output)
                if not new_keywords:
                    continue
                # Merge: new keywords first (higher relevance), then existing, dedup, cap at 40
                merged = list(dict.fromkeys(new_keywords + child.recent_keywords))[:40]
                if merged != child.recent_keywords:
                    child.recent_keywords = merged
                    enriched_any = True
            except Exception as e:
                logger.warning("Enrichment failed for %s: %s", self._item_label(item_id), e)

        if enriched_any and self._save_state_fn is not None:
            self._save_state_fn()
