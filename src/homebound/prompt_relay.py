"""Prompt relay manager — detects and relays runtime prompts from child sessions.

Extracted from orchestrator.py to keep prompt-relay logic self-contained.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from homebound.config import HomeboundConfig
from homebound.security import CommandAction, CommandPolicy, Principal
from homebound.session import ChildInfo, read_child_output, send_to_child

logger = logging.getLogger("homebound")


@dataclass
class PendingPrompt:
    """Pending runtime prompt detected from child output."""

    prompt_id: str
    item_id: int
    owner_user_id: str
    question_text: str
    options: list[str]
    created_at: float
    last_seen_hash: str
    status: str = "pending"
    last_seen_at: float = field(default_factory=time.time)


class PromptRelayManager:
    """Manages runtime prompt detection, relay, and answer resolution.

    Parameters
    ----------
    config:
        Full homebound config.
    children:
        Reference to orchestrator's children dict (shared, mutated externally).
    command_policy:
        Security policy for evaluating prompt-answer permissions.
    item_label_fn:
        Callable(item_id) -> str for formatting item labels (e.g. "Agent1").
    post_fn:
        Async callable(message) -> str to post messages to the transport.
    normalize_fn:
        Callable(text) -> str for normalizing command text.
    """

    def __init__(
        self,
        config: HomeboundConfig,
        children: dict[int, ChildInfo | None],
        command_policy: CommandPolicy,
        item_label_fn: Callable[[int], str],
        post_fn: Callable[..., Coroutine[Any, Any, str]],
        normalize_fn: Callable[[str], str],
    ) -> None:
        self.config = config
        self.children = children
        self.command_policy = command_policy
        self._item_label_fn = item_label_fn
        self._post_fn = post_fn
        self._normalize_fn = normalize_fn

        self._pending_prompts: dict[int, list[PendingPrompt]] = {}
        self._prompt_counter: int = 0
        self._prompt_option_patterns: list[re.Pattern] = [
            re.compile(pattern)
            for pattern in self.config.prompt_relay.option_patterns
        ]

    # --- Poll cycle counter (set externally by orchestrator) ---
    # The orchestrator's _poll_cycles is needed for scan_runtime_prompts,
    # so we accept it as a parameter there.

    def new_prompt_id(self, item_id: int) -> str:
        self._prompt_counter += 1
        return f"p-{item_id}-{int(time.time())}-{self._prompt_counter}"

    def active_prompts_for_item(self, item_id: int) -> list[PendingPrompt]:
        prompts = self._pending_prompts.get(item_id, [])
        return [prompt for prompt in prompts if prompt.status == "pending"]

    def all_active_prompts(self) -> list[PendingPrompt]:
        active: list[PendingPrompt] = []
        for item_id in sorted(self._pending_prompts.keys()):
            active.extend(self.active_prompts_for_item(item_id))
        return active

    def drop_pending_prompts_for_item(self, item_id: int) -> None:
        self._pending_prompts.pop(item_id, None)

    def expire_pending_prompts(self) -> None:
        ttl = self.config.prompt_relay.ttl_seconds
        now = time.time()
        for item_id in list(self._pending_prompts.keys()):
            active = [
                prompt for prompt in self.active_prompts_for_item(item_id)
                if (now - prompt.last_seen_at) <= ttl
            ]
            if active:
                self._pending_prompts[item_id] = active
            else:
                self._pending_prompts.pop(item_id, None)

    def detect_prompt_from_output(self, output: str) -> tuple[str, list[str]] | None:
        lines = [line.rstrip() for line in output.splitlines()]
        if not lines:
            return None

        def _extract_option_text(line: str) -> str | None:
            for pattern in self._prompt_option_patterns:
                match = pattern.match(line)
                if not match:
                    continue
                option_text = line.strip()
                if match.lastindex:
                    captured = match.group(1)
                    if captured:
                        option_text = captured.strip()
                option_text = option_text.strip()
                if option_text:
                    return option_text
            return None

        # Find the most recent option line first, then capture only that contiguous
        # option block to avoid merging historical prompt blocks.
        latest_option_idx: int | None = None
        for idx in range(len(lines) - 1, -1, -1):
            if _extract_option_text(lines[idx]) is not None:
                latest_option_idx = idx
                break
        if latest_option_idx is None:
            return None

        # Guard: the option block must be near the bottom of the output.
        # Real runtime prompts appear just before the cursor. If there are
        # substantive content lines after the options, this is just a
        # numbered list in Claude's regular response — not a prompt.
        max_trailing = 5  # allow a few blank/status/idle lines after options
        trailing_content = 0
        for idx in range(latest_option_idx + 1, len(lines)):
            stripped = lines[idx].strip()
            if stripped and not re.match(r'^[❯›>\s✻⏵⏺]*$', stripped):
                trailing_content += 1
        if trailing_content > max_trailing:
            return None

        options_rev: list[str] = []
        block_start = latest_option_idx
        idx = latest_option_idx
        while idx >= 0:
            option_text = _extract_option_text(lines[idx])
            if option_text is None:
                break
            options_rev.append(option_text)
            block_start = idx
            idx -= 1
        options = list(reversed(options_rev))
        if len(options) < 2:
            return None

        question_text = ""
        for qidx in range(block_start - 1, -1, -1):
            candidate = lines[qidx].strip()
            if not candidate:
                continue
            if self.config.prompt_relay.question_mark_required:
                if "?" in candidate:
                    question_text = candidate
                    break
                continue
            else:
                question_text = candidate
                break

        if not question_text:
            return None
        return question_text, options

    def build_prompt_hash(self, question_text: str, options: list[str]) -> str:
        normalized_question = self._normalize_fn(question_text)
        normalized_options = [self._normalize_fn(option) for option in options]
        joined = "|".join([normalized_question, *normalized_options])
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def resolve_prompt_answer(self, prompt: PendingPrompt, raw_value: str) -> tuple[str | None, str | None]:
        value = raw_value.strip()
        if not value:
            return None, "empty answer"

        # 1) Numeric selector: "1", "2)", "3.", "4:"
        number_match = re.fullmatch(r"(\d+)\s*[\)\.\:]?", value)
        if number_match:
            idx = int(number_match.group(1)) - 1
            if 0 <= idx < len(prompt.options):
                return prompt.options[idx], None
            # Keep free-text fallback behavior for non-matching numeric values.
            # This preserves operator intent for prompts that accept custom input.
            return value, None

        # 2) Letter selector: "A", "b)", "C.", "d:"
        letter_match = re.fullmatch(r"([A-Za-z])\s*[\)\.\:]?", value)
        if letter_match:
            idx = ord(letter_match.group(1).lower()) - ord("a")
            if 0 <= idx < len(prompt.options):
                return prompt.options[idx], None

        # 3) Exact option-text match (normalized)
        normalized = self._normalize_fn(value)
        for option in prompt.options:
            if self._normalize_fn(option) == normalized:
                return option, None

        # 4) Free-text fallback (intentional for this batch)
        return value, None

    def format_prompt_relay_message(self, prompt: PendingPrompt) -> str:
        options = "\n".join(
            f">{idx}. {option}" for idx, option in enumerate(prompt.options, start=1)
        )
        label = self._item_label_fn(prompt.item_id)
        return (
            f":bell: *{label} — Runtime prompt*\n\n"
            f"{prompt.question_text}\n\n"
            f"{options}\n\n"
            f"_Reply with_ `{label} ans <value>` _(number, letter, or option text)_"
        )

    async def scan_runtime_prompts(self, poll_cycles: int) -> None:
        relay = self.config.prompt_relay
        if not relay.enabled:
            return
        if (poll_cycles % relay.poll_every_cycles) != 0:
            return

        active_item_ids: set[int] = set()
        for item_id, child in self.children.items():
            if child is None:
                continue
            active_item_ids.add(item_id)
            try:
                output = await read_child_output(child, self.config, lines=relay.scan_lines)
            except Exception as e:
                logger.debug(
                    "%s: prompt scan skipped due to output read error: %s",
                    self._item_label_fn(item_id), e,
                )
                continue
            detected = self.detect_prompt_from_output(output)
            if not detected:
                continue

            question_text, options = detected
            prompt_hash = self.build_prompt_hash(question_text, options)
            now = time.time()
            active_prompts = self.active_prompts_for_item(item_id)

            deduped = False
            for pending in active_prompts:
                if pending.last_seen_hash == prompt_hash:
                    pending.last_seen_at = now
                    deduped = True
                    break
            if deduped:
                self._pending_prompts[item_id] = active_prompts
                continue

            prompt = PendingPrompt(
                prompt_id=self.new_prompt_id(item_id),
                item_id=item_id,
                owner_user_id=child.owner_user_id,
                question_text=question_text,
                options=options,
                created_at=now,
                last_seen_hash=prompt_hash,
                status="pending",
                last_seen_at=now,
            )
            active_prompts.append(prompt)
            max_pending = max(1, relay.max_pending_per_issue)
            if len(active_prompts) > max_pending:
                active_prompts = active_prompts[-max_pending:]
            self._pending_prompts[item_id] = active_prompts
            await self._post_fn(self.format_prompt_relay_message(prompt))

        for item_id in list(self._pending_prompts.keys()):
            if item_id not in active_item_ids:
                self._pending_prompts.pop(item_id, None)

    async def handle_prompt_answer(
        self,
        item_id: int,
        raw_value: str,
        principal: Principal,
        *,
        announce_denied: bool,
    ) -> bool:
        if not self.config.prompt_relay.enabled:
            if announce_denied:
                await self._post_fn("Runtime prompt relay is disabled.")
            return announce_denied

        decision = self.command_policy.evaluate(CommandAction.PROMPT_ANSWER, principal)
        if not decision.allow:
            if announce_denied:
                await self._post_fn("Prompt answer denied by policy.")
            return announce_denied

        prompts = self.active_prompts_for_item(item_id)
        label = self._item_label_fn(item_id)
        if not prompts:
            if announce_denied:
                await self._post_fn(f"{label}: No pending runtime prompt to answer.")
            return announce_denied
        prompt = prompts[-1]

        child = self.children.get(item_id)
        if child is None and item_id not in self.children:
            if announce_denied:
                await self._post_fn(f"{label}: No active session for this prompt.")
            self.drop_pending_prompts_for_item(item_id)
            return announce_denied
        if child is None:
            if announce_denied:
                await self._post_fn(f"{label}: Session is starting; try answering again shortly.")
            return announce_denied

        answer_value, error = self.resolve_prompt_answer(prompt, raw_value)
        if answer_value is None:
            if announce_denied:
                await self._post_fn(
                    f"{label}: Invalid answer ({error}). "
                    "Use a listed option number/letter/text."
                )
            return announce_denied

        await send_to_child(
            child,
            answer_value,
            self.config,
            raw=True,
        )
        child.idle_warnings = 0
        prompts = [p for p in prompts if p.prompt_id != prompt.prompt_id]
        if prompts:
            self._pending_prompts[item_id] = prompts
        else:
            self._pending_prompts.pop(item_id, None)
        await self._post_fn(f":white_check_mark: *{label}*: Prompt answer relayed to session.")
        return True

