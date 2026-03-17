"""Homebound Orchestrator — persistent process managing agent sessions.

Runs in a tmux window, monitors a transport (Slack) for commands, and
manages interactive agent sessions in dedicated tmux windows.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime

from homebound.adapters.transport import Transport
from homebound.admin import AdminCommandHandler, format_duration
from homebound.config import HomeboundConfig
from homebound.prompt_relay import PendingPrompt, PromptRelayManager
from homebound.routing import RoutingEngine
from homebound.security import CommandAction, CommandPolicy, Principal
from homebound.session import (
    ChildInfo,
    adopt_child,
    close_child,
    extract_keywords,
    parse_window_name,
    read_child_output,
    send_to_child,
    spawn_child,
)
from homebound.tmux import list_windows as tmux_list_windows

logger = logging.getLogger("homebound")


@dataclass
class StartupWatch:
    """Tracks first-turn startup visibility for newly spawned sessions."""

    started_at: float
    mode: str
    baseline_output_hash: str | None = None
    first_signal_seen: bool = False
    working_ping_sent: bool = False
    stuck_ping_sent: bool = False


class Orchestrator:
    """Main orchestrator: polls transport, routes messages, manages children."""

    def __init__(
        self,
        config: HomeboundConfig,
        dry_run: bool = False,
    ):
        self.config = config
        self.dry_run = dry_run

        self.children: dict[int, ChildInfo | None] = {}
        self.seen_ts: set[str] = set()
        self.shutting_down = False
        self.startup_ts: float = time.time()
        self._last_poll_ts: float = self.startup_ts

        # Lazy-init transport and tracker
        self._transport: Transport | None = None
        self._tracker = None
        self._command_policy = CommandPolicy(config.security)

        # State persistence
        log_dir = config.project_dir / "tmp" / config.name
        log_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = log_dir / "children.json"

        self._poll_cycles = 0
        self._startup_watch: dict[int, StartupWatch] = {}
        self._startup_working_seconds = 30
        self._startup_stuck_seconds = 180
        # Prompt relay manager (extracted module)
        self._prompt_relay = PromptRelayManager(
            config=config,
            children=self.children,
            command_policy=self._command_policy,
            item_label_fn=self._item_label,
            post_fn=lambda *a, **kw: self._post(*a, **kw),
            normalize_fn=self._normalize_command_text,
        )
        # Smart routing engine (thread, keyword, LLM routing)
        self._router = RoutingEngine(
            config,
            self.children,
            pending_prompts_fn=self._prompt_relay.active_prompts_for_item,
            save_state_fn=self._save_children_state,
        )
        # Admin command handler (extracted module)
        self._admin = AdminCommandHandler(
            config=config,
            children=self.children,
            command_policy=self._command_policy,
            tracker_fn=lambda: self.tracker,
            post_fn=lambda *a, **kw: self._post(*a, **kw),
            item_label_fn=self._item_label,
            normalize_fn=self._normalize_command_text,
            read_child_output_fn=read_child_output,
            strip_client_signature_fn=self._strip_client_signature,
            principal_from_fields_fn=self._principal_from_fields,
        )
        self._spawn_tasks: set[asyncio.Task] = set()
        self._consecutive_poll_failures: int = 0
        self._outage_start_time: float | None = None

    @property
    def command_policy(self) -> CommandPolicy:
        return self._command_policy

    @command_policy.setter
    def command_policy(self, policy: CommandPolicy) -> None:
        self._command_policy = policy
        self._admin.command_policy = policy
        self._prompt_relay.command_policy = policy

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = self.config.get_transport()
        return self._transport

    @property
    def tracker(self):
        if self._tracker is None:
            self._tracker = self.config.get_tracker()
        return self._tracker

    @property
    def max_children(self) -> int:
        return self.config.sessions.max_concurrent

    @property
    def poll_interval(self) -> int:
        return self.config.sessions.poll_interval

    # --- State persistence ---

    def _save_children_state(self) -> None:
        """Write current children to JSON for crash recovery."""
        state: dict = {"children": {}, "message_session_map": self._router.message_session_map}
        for item_id, child in self.children.items():
            if child is None:
                continue
            state["children"][str(item_id)] = {
                "window_name": child.window_name,
                "started_at": child.started_at.isoformat(),
                "last_message_at": child.last_message_at.isoformat(),
                "owner_user_id": child.owner_user_id,
                "topic_summary": child.topic_summary,
                "recent_keywords": child.recent_keywords,
                "posted_message_ts": child.posted_message_ts[-50:],
                "github_issue_id": child.github_issue_id,
            }
        try:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2))
            tmp.replace(self._state_file)
        except Exception as e:
            logger.warning("Failed to save children state: %s", e)

    def _load_children_state(self) -> dict:
        """Load saved children state for crash recovery."""
        if not self._state_file.exists():
            return {}
        try:
            raw = json.loads(self._state_file.read_text())
            children_data = raw["children"]
            self._router.message_session_map = raw.get("message_session_map", {})
            result = {}
            for k, v in children_data.items():
                entry: dict = {
                    "started_at": datetime.fromisoformat(v["started_at"]),
                    "last_message_at": datetime.fromisoformat(v["last_message_at"]),
                    "owner_user_id": v["owner_user_id"],
                    "topic_summary": v["topic_summary"],
                    "recent_keywords": v["recent_keywords"],
                    "posted_message_ts": v["posted_message_ts"],
                    "github_issue_id": v.get("github_issue_id"),
                }
                result[int(k)] = entry
            return result
        except Exception as e:
            logger.warning("Failed to load children state: %s", e)
            return {}

    # --- Security ---

    def _is_user_denied(self, user_id: str, msg_extra: dict | None = None) -> bool:
        """Check if a user is denied by the allowlist.

        Returns True if the user should be denied.
        """
        principal = self._principal_from_fields(user_id=user_id, extra=msg_extra)
        decision = self.command_policy.evaluate(CommandAction.INTAKE, principal)
        if not decision.allow:
            logger.debug("Denied message from %s: %s", user_id or "<anonymous>", decision.reason)
        return not decision.allow

    def _is_session_authorized(
        self, user_id: str, child: ChildInfo, msg_extra: dict | None = None,
    ) -> bool:
        """Check if user_id is authorized to interact with a child session."""
        principal = self._principal_from_fields(user_id=user_id, extra=msg_extra)
        decision = self.command_policy.evaluate(
            CommandAction.SESSION_ROUTE, principal, owner_user_id=child.owner_user_id,
        )
        return decision.allow

    @staticmethod
    def _normalize_command_text(command_text: str) -> str:
        return " ".join(command_text.strip().lower().split())

    def _item_label(self, item_id: int) -> str:
        return f"{self.config.sessions.agent_label}{item_id}"

    def _resolve_label_to_item_id(self, label: str) -> int | None:
        """Resolve a user-facing label (e.g. Agent1, Agent 1, or raw int) to a slot number."""
        dev_match = re.fullmatch(rf"(?:{re.escape(self.config.sessions.agent_label)})\s*(\d+)", label.strip(), re.IGNORECASE)
        if dev_match:
            return int(dev_match.group(1))
        try:
            return int(label.strip())
        except ValueError:
            return None

    def _status_hint(self, item_id: int) -> str:
        return f"Use `@{self.config.name} status` to inspect progress."

    def _parse_role_command(self, text: str) -> tuple[str, int | None, str] | None:
        match = re.match(rf"^@({re.escape(self.config.sessions.agent_label)})\s*(\d+)?\s+(.+)$", text.strip(), re.IGNORECASE)
        if not match:
            return None
        role = match.group(1).lower()
        slot_raw = match.group(2)
        payload = match.group(3).strip()
        slot = int(slot_raw) if slot_raw else None
        return role, slot, payload

    @staticmethod
    def _strip_client_signature(text: str) -> str:
        """Strip known client-appended footer text from incoming messages."""
        cleaned = text.strip()

        # Remove trailing line footers like "Sent using @Claude".
        lines = cleaned.splitlines()
        while lines:
            tail = lines[-1].strip()
            normalized_tail = re.sub(r"[*_`~]", "", tail).strip().lower()
            if normalized_tail.startswith("sent using"):
                lines.pop()
                continue
            break
        cleaned = "\n".join(lines).strip()

        # Remove inline trailing footers on the same line:
        #   "... *Sent using* @Claude"
        #   "... Sent using <@U123ABC>"
        cleaned = re.sub(
            r"(?:\s+|\n+)\*?sent using\*?\s+(?:@?claude(?:\s+desktop)?|<@[^>\s]+>|claude)(?:[.!]+)?\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        return cleaned

    @staticmethod
    def _hash_output(output: str) -> str:
        return hashlib.sha256(output.strip().encode("utf-8")).hexdigest()

    def _clear_startup_watch(self, item_id: int) -> None:
        self._startup_watch.pop(item_id, None)

    def _cleanup_session(self, item_id: int) -> None:
        """Remove a session and all associated state."""
        self.children.pop(item_id, None)
        self._prompt_relay.drop_pending_prompts_for_item(item_id)
        self._clear_startup_watch(item_id)
        self._save_children_state()

    def _mark_startup_signal(self, item_id: int, source: str) -> None:
        watch = self._startup_watch.get(item_id)
        if watch is None:
            return
        watch.first_signal_seen = True
        self._startup_watch.pop(item_id, None)
        logger.info("%s: startup signal observed from %s", self._item_label(item_id), source)

    def _extract_item_id_from_agent_message(self, text: str) -> int | None:
        prefix = re.escape(self.config.sessions.session_prefix.rstrip("-"))
        dev_match = re.search(rf"\[{prefix}-?(\d+)\b", text, re.IGNORECASE)
        if dev_match:
            return int(dev_match.group(1))
        return None

    def _record_agent_startup_signal(self, text: str, ts: str = "") -> None:
        item_id = self._extract_item_id_from_agent_message(text)
        if item_id is None:
            return
        self._mark_startup_signal(item_id, "agent message")
        # Record the message ts for thread-based routing
        if ts:
            self._router.record_outgoing_message(ts, item_id)

    async def _register_startup_watch(self, item_id: int, child: ChildInfo, mode: str) -> None:
        baseline_hash: str | None = None
        label = self._item_label(item_id)
        try:
            baseline_output = await read_child_output(child, self.config, lines=20)
            if baseline_output.strip():
                baseline_hash = self._hash_output(baseline_output)
        except Exception as e:
            logger.debug("%s: could not capture baseline startup output: %s", label, e)
        self._startup_watch[item_id] = StartupWatch(
            started_at=time.time(),
            mode=mode,
            baseline_output_hash=baseline_hash,
        )

    async def _check_startup_visibility(self) -> None:
        now = time.time()
        for item_id in list(self._startup_watch.keys()):
            watch = self._startup_watch.get(item_id)
            if watch is None:
                continue
            child = self.children.get(item_id)
            if child is None:
                self._clear_startup_watch(item_id)
                continue

            try:
                output = await read_child_output(child, self.config, lines=20)
            except Exception as e:
                logger.debug("%s: startup visibility output read failed: %s", self._item_label(item_id), e)
                output = ""

            if output.strip():
                current_hash = self._hash_output(output)
                if watch.baseline_output_hash is None:
                    watch.baseline_output_hash = current_hash
                elif current_hash != watch.baseline_output_hash:
                    self._mark_startup_signal(item_id, "tmux output")
                    continue

            elapsed = now - watch.started_at
            label = self._item_label(item_id)
            if (not watch.stuck_ping_sent) and elapsed >= self._startup_stuck_seconds:
                await self._post(
                    f":eyes: *{label}*: Session is still running but has no visible update yet. "
                    + self._status_hint(item_id)
                )
                watch.stuck_ping_sent = True
                watch.working_ping_sent = True
                continue

            if (not watch.working_ping_sent) and elapsed >= self._startup_working_seconds:
                await self._post(
                    f":gear: *{label}*: Session started and is still working on the initial request."
                )
                watch.working_ping_sent = True

    @staticmethod
    def _principal_from_fields(user_id: str, extra: dict | None = None) -> Principal:
        payload = extra or {}
        subtype = str(payload.get("subtype", "") or "")
        bot_id = str(payload.get("bot_id", "") or "")
        is_bot = bool(bot_id) or subtype == "bot_message"
        # For bot/webhook senders, prefer bot_id as stable identity when present.
        # Some payloads include both user and bot_id; allowlists should match bot_id.
        principal_id = bot_id if (is_bot and bot_id) else (user_id or bot_id)
        return Principal(user_id=principal_id, is_bot=is_bot)

    # --- Transport helpers ---

    async def _retry_transport(self, call, description: str):
        """Execute a transport call with retry backoff.

        Returns the call result, or raises the last exception on final failure.
        max_retries=0 means no retries but still makes the initial attempt.
        """
        max_retries = self.config.sessions.max_retries
        total_attempts = max_retries + 1
        last_error: Exception | None = None
        for attempt in range(total_attempts):
            try:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, call)
            except Exception as e:
                last_error = e
                base_delay = 2 ** (attempt + 1)
                delay = base_delay + random.uniform(0, base_delay * 0.25)
                logger.warning(
                    "Transport %s attempt %d/%d failed: %s (retry in %.0fs)",
                    description, attempt + 1, total_attempts, e, delay,
                )
                if attempt < total_attempts - 1:
                    await asyncio.sleep(delay)
        assert last_error is not None  # guaranteed: loop ran at least once
        raise last_error

    async def _post(
        self, message: str, thread_ts: str = "", item_id: int | None = None,
    ) -> str:
        """Post to transport with retry backoff.

        Returns the posted message's ts (empty string on failure).
        """
        posted_ts = ""
        try:
            posted_ts = await self._retry_transport(
                lambda: self.transport.post(message, thread_ts=thread_ts), "post",
            )
        except Exception:
            logger.error("Transport post failed after retries: %s", message[:100])
        if posted_ts:
            self._router.record_recent_message(self.config.name, message[:300])
            if item_id is not None:
                self._router.record_outgoing_message(posted_ts, item_id)
        return posted_ts

    # --- Transport health ---

    async def _update_transport_health(self, transport_ok: bool) -> None:
        """Track consecutive transport failures and notify on recovery."""
        if transport_ok:
            if self._consecutive_poll_failures >= self.config.sessions.outage_threshold:
                elapsed = time.time() - (self._outage_start_time or time.time())
                minutes, seconds = divmod(int(elapsed), 60)
                duration = f"{minutes}m{seconds}s" if minutes else f"{seconds}s"
                logger.info(
                    "Transport recovered after %d consecutive failures (%s)",
                    self._consecutive_poll_failures, duration,
                )
                await self._post(
                    f":large_green_circle: *{self.config.name} back online*\n"
                    f"Transport was unreachable for ~`{duration}` "
                    f"({self._consecutive_poll_failures} poll cycles failed)"
                )
            self._consecutive_poll_failures = 0
            self._outage_start_time = None
        else:
            self._consecutive_poll_failures += 1
            if self._consecutive_poll_failures == 1:
                self._outage_start_time = time.time()
            if self._consecutive_poll_failures == self.config.sessions.outage_threshold:
                logger.warning(
                    "Transport unreachable for %d consecutive cycles, "
                    "entering extended backoff",
                    self._consecutive_poll_failures,
                )

    def _effective_poll_delay(self) -> float:
        """Compute sleep duration between poll cycles, with backoff during outages."""
        base = self.config.sessions.poll_interval
        threshold = self.config.sessions.outage_threshold
        failures = self._consecutive_poll_failures

        if failures < threshold:
            return base + random.uniform(0, 1.0)

        max_interval = self.config.sessions.outage_max_interval
        steps = failures - threshold
        extended = min(base * (2.0 ** (steps + 1)), max_interval)
        return extended + random.uniform(0, extended * 0.1)

    # --- Main loop ---

    async def run(self) -> None:
        """Main loop: poll transport, route messages, health-check children."""
        logger.info(
            "%s starting (max_children=%d, poll=%ds, dry_run=%s)",
            self.config.name, self.max_children, self.poll_interval, self.dry_run,
        )

        adopted = await self._adopt_orphans()

        parts = [f":large_green_circle: *{self.config.name} online*"]
        parts.append(f"Max {self.max_children} concurrent sessions | Polling every {self.poll_interval}s")
        if adopted:
            parts.append(f":recycle: Re-adopted {len(adopted)} session(s): {', '.join(adopted)}")
        if self.config.security.allowed_users:
            parts.append(f":lock: Allowlist: {len(self.config.security.allowed_users)} user(s)")
        elif self.config.security.allow_open_channel:
            parts.append(":unlock: Open-channel mode enabled")
        else:
            parts.append(
                ":warning: Allowlist empty, open-channel disabled — "
                "commands denied until security is configured"
            )
        if self.dry_run:
            parts.append(":test_tube: *DRY RUN*")
        startup_msg = "\n".join(parts)
        await self._post(startup_msg)

        while not self.shutting_down:
            try:
                await self._poll_cycle()
            except Exception as e:
                logger.error("Poll cycle error: %s", e, exc_info=True)
            await asyncio.sleep(self._effective_poll_delay())

        await self._shutdown()

    async def _poll_cycle(self) -> None:
        """Single iteration: check transport messages and child health."""
        self._poll_cycles += 1
        self._prompt_relay.expire_pending_prompts()
        lookback = self.config.transport.lookback_minutes
        since_ts = min(self._last_poll_ts, time.time() - (lookback * 60))
        poll_limit = self.config.transport.poll_limit

        transport_ok = False
        messages = []
        try:
            messages = await self._retry_transport(
                lambda: self.transport.poll(since_ts=since_ts, limit=poll_limit),
                "poll",
            )
            transport_ok = True
        except Exception as e:
            logger.error("Transport poll failed after retries: %s", e)

        active_threads = self._router.active_thread_parents()
        if active_threads:
            try:
                thread_replies = await self._retry_transport(
                    lambda: self.transport.poll_thread_replies(active_threads, since_ts=since_ts),
                    "poll_thread_replies",
                )
                messages.extend(thread_replies)
                transport_ok = True
            except Exception as e:
                logger.error("Thread reply poll failed: %s", e)

        await self._update_transport_health(transport_ok)
        if transport_ok:
            self._last_poll_ts = time.time()

        for msg in messages:
            ts = msg.ts
            text = msg.text

            if not ts:
                continue

            try:
                if float(ts) < self.startup_ts:
                    continue
            except ValueError:
                continue

            if ts in self.seen_ts:
                continue
            self.seen_ts.add(ts)

            # Record agent messages for LLM routing context (always trusted)
            is_agent = self.transport.is_from_agent(msg, self.config.ignored_prefixes)
            if is_agent:
                self._router.record_recent_message("agent", text)

            # Skip agent-origin messages (prevents loops), but harvest agent activity
            # as a startup visibility signal for newly spawned sessions.
            if is_agent:
                self._record_agent_startup_signal(text, ts=ts)
                continue

            # User allowlist check
            sender = msg.user
            principal = self._principal_from_fields(sender, msg.extra)
            decision = self.command_policy.evaluate(CommandAction.INTAKE, principal)
            if not decision.allow:
                logger.debug(
                    "Denied message from %s: %s",
                    principal.user_id or "<anonymous>",
                    decision.reason,
                )
                continue

            # Record allowed user messages for LLM routing context
            self._router.record_recent_message("user", text)
            sender_id = principal.user_id

            # Strip recognized friendly prefixes
            stripped = text
            for prefix in self.config.transport.strip_prefixes:
                stripped = re.sub(
                    rf"^\[{re.escape(prefix)}\]\s*", "", stripped, flags=re.IGNORECASE
                )
            stripped = self._strip_client_signature(stripped)

            # Check for admin commands before item routing
            admin_match = re.search(self.config.admin_pattern, stripped, re.IGNORECASE)
            if admin_match:
                await self._admin.handle_admin_query(
                    admin_match.group(1).strip(), sender_user_id=sender_id, sender_extra=msg.extra,
                )
                continue

            prompt_answer_match = re.search(
                rf"^((?:{re.escape(self.config.sessions.agent_label)})?\s*\d+)\s+ans\s+(.+)$", stripped, re.IGNORECASE,
            )
            if prompt_answer_match:
                resolved = self._resolve_label_to_item_id(prompt_answer_match.group(1))
                if resolved is not None:
                    await self._prompt_relay.handle_prompt_answer(
                        resolved,
                        prompt_answer_match.group(2).strip(),
                        principal,
                        announce_denied=True,
                    )
                    continue

            # @<agent_label> command handling
            role_command = self._parse_role_command(stripped)
            if role_command:
                role, slot, payload = role_command
                if slot is not None:
                    # @Agent1 <task> — route to specific slot
                    if slot < 1 or slot > self.max_children:
                        await self._post(f"Slot must be between 1 and {self.max_children}.")
                        continue
                    await self._handle_issue_message(
                        slot, payload, sender_user_id=sender_id, sender_extra=msg.extra,
                    )
                    continue
                else:
                    # @Agent <task> — use stripped payload, fall through to Tier 2-4 cascade
                    stripped = payload

            bare_role_match = re.match(rf"^@({re.escape(self.config.sessions.agent_label)})\s*\d*$", stripped, re.IGNORECASE)
            if bare_role_match:
                al = self.config.sessions.agent_label
                await self._post(f"Usage: `@{al} <task>` or `@{al}1 <task>` to target a specific slot.")
                continue

            if not stripped:
                continue

            # Bare admin keywords (help, status) without @homebound prefix
            if stripped.lower() in ("help", "status", "sessions"):
                await self._admin.handle_admin_query(
                    stripped, sender_user_id=sender_id, sender_extra=msg.extra,
                )
                continue

            # --- Smart routing cascade ---

            # Tier 1: Thread-based routing
            if self.config.routing.thread_routing:
                thread_item_id = self._router.route_by_thread(msg)
                if thread_item_id is not None:
                    child = self.children[thread_item_id]
                    route_decision = self.command_policy.evaluate(
                        CommandAction.SESSION_ROUTE, principal,
                        owner_user_id=child.owner_user_id,
                    )
                    if route_decision.allow:
                        await send_to_child(
                            child, stripped, self.config,
                            thread_ts=msg.thread_ts,
                        )
                        child.active_thread_ts = msg.thread_ts
                        child.idle_warnings = 0
                        label = self._item_label(thread_item_id)
                        await self._post(
                            f":speech_balloon: *{label}*: Routed via thread reply",
                            thread_ts=msg.thread_ts, item_id=thread_item_id,
                        )
                        logger.info("Thread-routed message to %s", label)
                        continue
                    else:
                        label = self._item_label(thread_item_id)
                        logger.debug("Thread route to %s denied for %s", label, sender_id)
                        continue  # Block — don't fall through to keyword/LLM

            # Tier 2: Keyword-based matching
            if self.config.routing.keyword_routing and self.children:
                kw_item_id = self._router.match_by_keywords(stripped)
                if kw_item_id is not None:
                    child = self.children[kw_item_id]
                    route_decision = self.command_policy.evaluate(
                        CommandAction.SESSION_ROUTE, principal,
                        owner_user_id=child.owner_user_id,
                    )
                    if route_decision.allow:
                        await send_to_child(child, stripped, self.config)
                        child.idle_warnings = 0
                        label = self._item_label(kw_item_id)
                        await self._post(
                            f":mag: *{label}*: Routed via keyword match",
                            item_id=kw_item_id,
                        )
                        logger.info("Keyword-routed message to %s", label)
                        continue
                    else:
                        label = self._item_label(kw_item_id)
                        logger.debug("Keyword route to %s denied for %s", label, sender_id)

            # Tier 3: LLM-based matching
            if self.config.routing.llm_routing and self.children:
                llm_item_id = await self._router.match_by_llm(stripped)
                if llm_item_id is not None:
                    child = self.children[llm_item_id]
                    route_decision = self.command_policy.evaluate(
                        CommandAction.SESSION_ROUTE, principal,
                        owner_user_id=child.owner_user_id,
                    )
                    if route_decision.allow:
                        await send_to_child(child, stripped, self.config)
                        child.idle_warnings = 0
                        label = self._item_label(llm_item_id)
                        await self._post(
                            f":dart: *{label}*: Routed via smart match",
                            item_id=llm_item_id,
                        )
                        logger.info("LLM-routed message to %s", label)
                        continue
                    else:
                        label = self._item_label(llm_item_id)
                        logger.debug("LLM route to %s denied for %s", label, sender_id)

            # Tier 4: Auto-spawn new session for unmatched messages
            if self.config.routing.auto_spawn_on_no_match:
                slot = self._router.next_free_slot()
                if slot is not None:
                    await self._handle_issue_message(
                        slot, stripped,
                        sender_user_id=sender_id, sender_extra=msg.extra,
                    )
                    continue

            # Final fallback: admin/router command handling.
            await self._admin.handle_admin_query(
                stripped, sender_user_id=sender_id, sender_extra=msg.extra,
            )

        # Health-check children even when poll fails.
        await self._health_check()
        await self._check_startup_visibility()
        await self._prompt_relay.scan_runtime_prompts(self._poll_cycles)
        await self._router.maybe_enrich_session_context()

        # Prune seen_ts
        if len(self.seen_ts) > 1000:
            def _safe_float(x: str) -> float:
                try:
                    return float(x)
                except (ValueError, TypeError):
                    return 0.0
            self.seen_ts = set(sorted(self.seen_ts, key=_safe_float)[-700:])

    async def _handle_issue_message(
        self, item_id: int, task_text: str, sender_user_id: str = "",
        sender_extra: dict | None = None,
    ) -> None:
        """Route an item message to an existing child or spawn a new one."""
        max_msg_len = self.config.sessions.max_message_len
        principal = self._principal_from_fields(sender_user_id, sender_extra)
        label = self._item_label(item_id)

        # Close commands
        if task_text.strip().lower() in self.config.close_commands:
            close_decision = self.command_policy.evaluate(CommandAction.SESSION_CLOSE, principal)
            if not close_decision.allow:
                logger.debug("%s: close denied for user %s (%s)", label, sender_user_id, close_decision.reason)
                return
            child = self.children.get(item_id)
            if child is None and item_id not in self.children:
                await self._post(f"{label}: No active session to close.")
                return
            if child is None:
                await self._post(f"{label}: Session is still starting, try again shortly.")
                return
            close_owner_decision = self.command_policy.evaluate(
                CommandAction.SESSION_CLOSE, principal, owner_user_id=child.owner_user_id,
            )
            if not close_owner_decision.allow:
                logger.debug("%s: close denied for user %s (%s)", label, sender_user_id, close_owner_decision.reason)
                return
            if self.dry_run:
                logger.info("[DRY RUN] Would close %s", label)
                await self._post(f"{label}: [DRY RUN] Would close session.")
                return
            await self._post(f":hourglass_flowing_sand: *{label}*: Closing session…")
            await close_child(child, self.config)
            self._cleanup_session(item_id)
            await self._post(f":white_check_mark: *{label}*: Session closed. Slot freed.")
            return

        # Route to existing child
        if item_id in self.children:
            child = self.children[item_id]
            if child is None:
                logger.info("%s: spawn already in progress, ignoring", label)
                return
            route_decision = self.command_policy.evaluate(
                CommandAction.SESSION_ROUTE, principal, owner_user_id=child.owner_user_id,
            )
            if not route_decision.allow:
                logger.debug("%s: follow-up denied for user %s (%s)", label, sender_user_id, route_decision.reason)
                return
            if self.dry_run:
                logger.info("[DRY RUN] Would route to %s: %s", label, task_text[:100])
                return
            truncated = len(task_text) > max_msg_len
            await send_to_child(child, task_text, self.config)
            child.idle_warnings = 0
            msg = f":arrow_right: *{label}*: Routed to active session."
            if truncated:
                msg += f"\n:warning: Message truncated (`{len(task_text)}` chars > `{max_msg_len}` limit)"
            await self._post(msg, item_id=item_id)
            logger.info("Routed message to %s", label)
        else:
            # Spawn new session
            spawn_decision = self.command_policy.evaluate(CommandAction.SESSION_SPAWN, principal)
            if not spawn_decision.allow:
                logger.debug("%s: spawn denied for user %s (%s)", label, sender_user_id, spawn_decision.reason)
                return
            if len(self.children) >= self.max_children:
                await self._post(
                    f":no_entry_sign: *{label}*: At capacity (`{len(self.children)}/{self.max_children}`). "
                    f"Please resend when a slot opens."
                )
                return

            # Determine mode from keywords (unified pool).
            mode = self.config.default_mode
            for mode_name, mode_cfg in self.config.modes.items():
                if mode_cfg.keyword and task_text.lower().startswith(mode_cfg.keyword):
                    mode = mode_name
                    task_text = task_text[len(mode_cfg.keyword):].strip()
                    break

            if self.dry_run:
                logger.info("[DRY RUN] Would spawn for %s (%s)", label, mode)
                await self._post(f"{label}: [DRY RUN] Would spawn new session ({mode}).")
                return

            # Reserve slot with sentinel
            self.children[item_id] = None
            task = asyncio.create_task(
                self._do_spawn(item_id, task_text, mode, sender_user_id, label),
                name=f"spawn-{item_id}",
            )
            self._spawn_tasks.add(task)
            task.add_done_callback(self._on_spawn_done)
            logger.info("Spawn task created for %s (mode=%s)", label, mode)

    async def _do_spawn(
        self, item_id: int, task_text: str, mode: str,
        sender_user_id: str, label: str,
    ) -> None:
        """Background task: spawn child session. Sentinel already set by caller."""
        try:
            child = await spawn_child(
                item_id, task_text, config=self.config, mode=mode,
            )
            child.owner_user_id = sender_user_id
            child.topic_summary = task_text[:200]
            child.recent_keywords = extract_keywords(task_text)
            issue_match = re.search(r"#(\d+)\b", task_text)
            if issue_match:
                child.github_issue_id = int(issue_match.group(1))
            self.children[item_id] = child
            await self._register_startup_watch(item_id, child, mode)
            self._save_children_state()
            await self._post(f":rocket: *{label}*: New session started (`{mode}`)", item_id=item_id)
            logger.info("Spawned child for %s (mode=%s)", label, mode)
        except Exception as e:
            self._cleanup_session(item_id)
            await self._post(f":rotating_light: *{label}*: Failed to spawn session — `{e}`")
            logger.error("Failed to spawn for %s: %s", label, e, exc_info=True)

    def _on_spawn_done(self, task: asyncio.Task) -> None:
        """Callback when a background spawn task completes."""
        self._spawn_tasks.discard(task)
        if task.cancelled():
            logger.info("Spawn task %s was cancelled", task.get_name())
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Unexpected error in spawn task %s: %s", task.get_name(), exc)

    # Keep _format_duration as a static alias for backward compatibility
    _format_duration = staticmethod(format_duration)

    async def _health_check(self) -> None:
        """Check all children for staleness and verify they're still alive."""
        if not self.children:
            return
        idle_markers = self.config.runtime.idle_markers
        idle_timeout = self.config.sessions.idle_timeout
        threshold = self.config.sessions.idle_warning_threshold

        # Fetch window list once for all children
        try:
            windows = await tmux_list_windows(self.config.tmux_session_name)
        except Exception:
            windows = []
        window_set = set(windows)

        for item_id, child in list(self.children.items()):
            if child is None:
                continue
            label = self._item_label(item_id)

            alive = child.window_name in window_set
            if not alive:
                logger.warning("%s: tmux window disappeared", label)
                await self._post(f":octagonal_sign: *{label}*: Session ended (window closed)")
                self._cleanup_session(item_id)
                continue

            if child.is_stale(idle_timeout):
                output = await read_child_output(child, self.config, lines=5)
                last_lines = output.strip().splitlines()[-3:] if output.strip() else []

                at_prompt = any(
                    any(marker in line for marker in idle_markers)
                    for line in last_lines
                )

                if at_prompt:
                    child.idle_warnings += 1
                    idle_mins = int(
                        (datetime.now() - child.last_message_at).total_seconds() / 60
                    )

                    if child.idle_warnings >= threshold:
                        await self._post(
                            f":zzz: *{label}*: Auto-closing after `{idle_mins}m` idle "
                            f"({child.idle_warnings} warnings). Freeing slot."
                        )
                        logger.info("%s auto-closed after %d idle warnings", label, child.idle_warnings)
                        await close_child(child, self.config)
                        self._cleanup_session(item_id)
                    else:
                        remaining = threshold - child.idle_warnings
                        await self._post(
                            f":hourglass: *{label}*: Session idle for `{idle_mins}m` — "
                            f"work appears complete. Will auto-close after "
                            f"{remaining} more warning(s)."
                        )
                        child.last_message_at = datetime.now()
                else:
                    if child.idle_warnings > 0:
                        child.idle_warnings = 0

    async def _adopt_orphans(self) -> list[str]:
        """Scan tmux for orphaned child windows and re-adopt them."""
        adopted: list[str] = []
        saved_state = self._load_children_state()

        try:
            windows = await tmux_list_windows(self.config.tmux_session_name)
        except Exception as e:
            logger.warning("Could not list tmux windows for orphan scan: %s", e)
            return adopted

        for wname in windows:
            item_id = parse_window_name(wname, self.config)
            if item_id is None:
                continue

            if item_id in self.children:
                continue

            try:
                child = await adopt_child(item_id, self.config, known_windows=windows)
                if item_id in saved_state:
                    state = saved_state[item_id]
                    child.started_at = state["started_at"]
                    if "last_message_at" in state:
                        child.last_message_at = state["last_message_at"]
                    child.owner_user_id = state.get("owner_user_id", "")
                    child.topic_summary = state.get("topic_summary", "")
                    child.recent_keywords = state.get("recent_keywords", [])
                    child.posted_message_ts = state.get("posted_message_ts", [])
                    child.github_issue_id = state.get("github_issue_id")
                self.children[item_id] = child
                adopted.append(self._item_label(item_id))
                logger.info("Re-adopted orphan session %s", self._item_label(item_id))
            except Exception as e:
                logger.error("Failed to adopt %s: %s", self._item_label(item_id), e)

        if adopted:
            self._save_children_state()
        return adopted

    async def _shutdown(self) -> None:
        """Graceful shutdown — preserve child sessions."""
        if self._spawn_tasks:
            logger.info("Waiting for %d in-flight spawn(s)…", len(self._spawn_tasks))
            _done, pending = await asyncio.wait(self._spawn_tasks, timeout=10)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        active = [self._item_label(n) for n, c in self.children.items() if c is not None]
        if active:
            logger.info(
                "Shutting down. %d session(s) continue: %s",
                len(active), ", ".join(active),
            )
            await self._post(
                f":red_circle: *{self.config.name} offline*\n"
                f"{len(active)} session(s) continue: {', '.join(active)}\n"
                f"Use `stop-all` to tear down everything."
            )
        else:
            await self._post(f":red_circle: *{self.config.name} offline*")
        logger.info("Shutdown complete.")

    def request_shutdown(self) -> None:
        """Signal the main loop to stop (called from signal handler)."""
        logger.info("Shutdown requested")
        self.shutting_down = True
