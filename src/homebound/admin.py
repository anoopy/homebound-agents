"""Admin command handling for the Homebound orchestrator.

Extracted from orchestrator.py to keep the Orchestrator class focused on
session lifecycle and transport polling.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from homebound.adapters.tracker import CommandLevel
from homebound.security import CommandAction, CommandPolicy, Principal
from homebound.session import ChildInfo, list_custom_skills

if TYPE_CHECKING:
    from homebound.config import HomeboundConfig

logger = logging.getLogger("homebound")


def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_mins = minutes % 60
    return f"{hours}h{remaining_mins:02d}m"


class AdminCommandHandler:
    """Handles admin/operator commands: status, help, skills, issue queries, tracker."""

    def __init__(
        self,
        config: HomeboundConfig,
        children: dict[int, ChildInfo | None],
        command_policy: CommandPolicy,
        tracker_fn: Callable[[], Any],
        post_fn: Callable[..., Coroutine],
        item_label_fn: Callable[[int], str],
        normalize_fn: Callable[[str], str],
        read_child_output_fn: Callable[..., Coroutine],
        strip_client_signature_fn: Callable[[str], str],
        principal_from_fields_fn: Callable[..., Principal],
    ):
        self.config = config
        self.children = children
        self.command_policy = command_policy
        self._tracker_fn = tracker_fn
        self._post = post_fn
        self._item_label = item_label_fn
        self._normalize_command_text = normalize_fn
        self._read_child_output = read_child_output_fn
        self._strip_client_signature = strip_client_signature_fn
        self._principal_from_fields = principal_from_fields_fn

        # Pending destructive confirmations: {(user_id, command): timestamp}
        self._pending_confirms: dict[tuple[str, str], float] = {}

    @property
    def tracker(self) -> Any:
        return self._tracker_fn()

    async def handle_admin_query(
        self, command_text: str, sender_user_id: str = "", sender_extra: dict | None = None,
    ) -> None:
        """Handle an admin command."""
        command_text = self._strip_client_signature(command_text).strip()
        if not command_text:
            return
        cmd_lower = command_text.lower()
        normalized_cmd = " ".join(cmd_lower.split()).rstrip("?.!;:")
        if re.fullmatch(
            r"(?:which|what)\s+sessions?\s+(?:are\s+)?open(?:\s+(?:right\s+)?now)?",
            normalized_cmd,
        ):
            command_text = "status"
            cmd_lower = "status"
        if re.fullmatch(r"what(?:'s| is)\s+open(?:\s+right\s+now)?", normalized_cmd):
            command_text = "status"
            cmd_lower = "status"
        principal = self._principal_from_fields(sender_user_id, sender_extra)

        if cmd_lower in ("status", "sessions"):
            status_decision = self.command_policy.evaluate(CommandAction.ADMIN_STATUS, principal)
            if not status_decision.allow:
                logger.debug("Admin status denied for user %s (%s)", sender_user_id, status_decision.reason)
                return
            await self.report_sessions()
            return

        if cmd_lower == "help":
            help_decision = self.command_policy.evaluate(CommandAction.ADMIN_HELP, principal)
            if not help_decision.allow:
                logger.debug("Admin help denied for user %s (%s)", sender_user_id, help_decision.reason)
                return
            await self.post_admin_help()
            return

        if cmd_lower == "skills":
            skills_decision = self.command_policy.evaluate(CommandAction.ADMIN_SKILLS, principal)
            if not skills_decision.allow:
                logger.debug("Admin skills denied for user %s (%s)", sender_user_id, skills_decision.reason)
                return
            await self.post_admin_skills()
            return

        # Issue status query: N?
        issue_match = re.search(
            self.config.tracker.item_query_pattern, command_text,
        )
        if issue_match:
            issue_status_decision = self.command_policy.evaluate(CommandAction.ADMIN_ISSUE_STATUS, principal)
            if not issue_status_decision.allow:
                logger.debug("Admin issue status denied for user %s (%s)", sender_user_id, issue_status_decision.reason)
                return
            item_id = int(issue_match.group(1))
            await self.report_issue_status(item_id)
            return

        # Fallthrough: tracker commands
        classified = self.tracker.classify(command_text)
        if classified is not None:
            action = {
                CommandLevel.READ: CommandAction.TRACKER_READ,
                CommandLevel.WRITE: CommandAction.TRACKER_WRITE,
                CommandLevel.DESTRUCTIVE: CommandAction.TRACKER_DESTRUCTIVE,
            }[classified.level]
            decision = self.command_policy.evaluate(action, principal)
            if not decision.allow:
                logger.debug(
                    "Tracker command denied for user %s (%s): %s",
                    sender_user_id, decision.reason, command_text[:80],
                )
                return
            if classified.level == CommandLevel.DESTRUCTIVE:
                normalized_command = self._normalize_command_text(command_text)
                confirm_key = (sender_user_id, normalized_command)
                now = time.time()
                timeout = self.config.security.destructive_confirm_timeout
                self._pending_confirms = {
                    k: v for k, v in self._pending_confirms.items()
                    if (now - v) < timeout
                }
                prev_ts = self._pending_confirms.get(confirm_key)
                if prev_ts is not None and (now - prev_ts) < timeout:
                    del self._pending_confirms[confirm_key]
                    result = await self.tracker.execute(classified)
                    if result.success:
                        await self._post(f"```\n{result.output[:3000]}\n```")
                    else:
                        await self._post(f"Error: {result.error[:500]}")
                else:
                    self._pending_confirms[confirm_key] = now
                    await self._post(
                        f":warning: *Destructive command:* {classified.description}\n"
                        f"Repeat the same command within `{timeout}s` to confirm."
                    )
            else:
                result = await self.tracker.execute(classified)
                if result.success:
                    await self._post(f"```\n{result.output[:3000]}\n```")
                else:
                    await self._post(f"Error: {result.error[:500]}")
            return

        await self._post(
            f":thinking_face: Unknown command: `{command_text[:80]}`\n"
            f"Try `@{self.config.name} help` for available commands."
        )

    async def report_sessions(self) -> None:
        """Report status of all active child sessions."""
        max_children = self.config.sessions.max_concurrent
        active = {n: c for n, c in self.children.items() if c is not None}
        total = len(active)
        if total == 0:
            await self._post(f":clipboard: Active sessions (`0/{max_children}`): _none_")
            return

        lines = [f":clipboard: *Active sessions* (`{total}/{max_children}`):"]
        # Show pool summary in multi-runtime mode
        if self.config.is_multi_runtime:
            pool_counts: dict[str, int] = {}
            for c in active.values():
                pn = c.pool_name or "default"
                pool_counts[pn] = pool_counts.get(pn, 0) + 1
            pool_summary = ", ".join(f"{p}: {n}" for p, n in sorted(pool_counts.items()))
            lines.append(f"Pools: {pool_summary}")
        now = datetime.now()
        for item_id, child in sorted(active.items()):
            uptime_secs = (now - child.started_at).total_seconds()
            idle_secs = (now - child.last_message_at).total_seconds()
            uptime_str = format_duration(uptime_secs)
            idle_str = format_duration(idle_secs)

            last_line = ""
            try:
                output = await self._read_child_output(child, self.config, lines=3)
                output_lines = output.strip().splitlines()
                if output_lines:
                    last_line = output_lines[-1].strip()[:80]
            except Exception:
                last_line = "(read error)"

            # Short topic blurb: first ~50 chars of topic_summary, cut at word boundary
            topic = child.topic_summary.strip() if child.topic_summary else "(adopted session)"
            if len(topic) > 50:
                topic = topic[:50].rsplit(" ", 1)[0] + "…"
            topic_tag = f" — _{topic}_" if topic else ""
            lines.append(
                f">*{self._item_label(item_id)}*{topic_tag}\n"
                f">  up `{uptime_str}`, idle `{idle_str}` · `{last_line}`"
            )
        await self._post("\n".join(lines))

    async def report_issue_status(self, item_id: int) -> None:
        """Report detailed status for a specific issue session."""
        label = self._item_label(item_id)
        child = self.children.get(item_id)
        if child is None and item_id not in self.children:
            await self._post(f"No active session for {label}.")
            return
        if child is None:
            await self._post(f"{label}: Session is still starting.")
            return

        output = await self._read_child_output(child, self.config, lines=30)
        if not output.strip():
            await self._post(f"{label}: Session active but no recent output.")
            return

        stripped = output.strip()
        if len(stripped) > 1500:
            truncated = "... (truncated)\n" + stripped[-1500:]
        else:
            truncated = stripped
        await self._post(f":mag: *{label}* recent output:\n```\n{truncated}\n```")

    async def post_admin_help(self) -> None:
        """Post available admin commands."""
        aliases = [self.config.name] + self.config.orchestrator.aliases
        short = aliases[-1] if len(aliases) > 1 else aliases[0]

        # Build session command examples based on configured pools
        if self.config.is_multi_runtime:
            pool_examples = []
            for pool in self.config.pool_names:
                lbl = self.config.pool_label(pool)
                pool_examples.append(
                    f">`@{lbl} <task>` — spawn `{pool}` session in next free slot\n"
                    f">`@{lbl}1 <task>` — route to specific `{pool}` slot\n"
                    f">`@{lbl}1 close` — close session\n"
                    f">`{lbl}1 ans <value>` — answer runtime prompt"
                )
            session_lines = "\n".join(pool_examples)
            pools_note = (
                "\n\n*Pools:* "
                + ", ".join(
                    f"`{p}` ({self.config.runtimes[p].type})"
                    for p in self.config.pool_names
                )
            )
        else:
            agent = self.config.sessions.agent_label
            session_lines = (
                f">`@{agent} <task>` — spawn or route to next free slot\n"
                f">`@{agent}1 <task>` — route to specific slot (any number)\n"
                f">`@{agent}1 close` — close session\n"
                f">`{agent}1 ans <value>` — answer runtime prompt"
            )
            pools_note = ""

        await self._post(
            ":information_source: *Commands*\n\n"
            "*Sessions*\n"
            ">`<text>` — smart-routed to matching session or new slot\n"
            f"{session_lines}\n\n"
            "*Admin*\n"
            f">`status` or `@{short} status` — list sessions\n"
            f">`help` or `@{short} help` — this help\n"
            f">`@{short} skills` — list available skills\n"
            f">`@{short} <N>?` — issue status"
            f"{pools_note}"
        )

    async def post_admin_skills(self) -> None:
        """Post available custom skills from project and user .claude/skills/ directories."""
        skills = list_custom_skills(self.config.project_dir)
        if not skills:
            await self._post("No custom skills found.")
            return
        lines = [f":toolbox: *Available skills* ({len(skills)}):"]
        for name, desc in skills:
            lines.append(f">`/{name}` — {desc}")
        await self._post("\n".join(lines))
