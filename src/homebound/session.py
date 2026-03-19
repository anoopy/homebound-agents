"""Child session lifecycle management via tmux.

Each child is an interactive CLI process running in a dedicated
tmux window. This module handles spawning, messaging, reading output,
and graceful shutdown of child sessions.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from homebound.config import HomeboundConfig
from homebound.tmux import (
    capture_pane,
    kill_window,
    list_windows,
    new_window,
    run_tmux,
    send_keys,
    wait_for_prompt,
)

logger = logging.getLogger("homebound.session")

@dataclass
class ChildInfo:
    """Tracks a running child agent session."""

    item_id: int  # Slot number (1..max_concurrent) in unified pool
    window_name: str  # e.g. "AGENT-1" or "CLAUDE-1"
    started_at: datetime = field(default_factory=datetime.now)
    last_message_at: datetime = field(default_factory=datetime.now)
    idle_warnings: int = 0
    owner_user_id: str = ""  # User who spawned this session
    topic_summary: str = ""  # Short description of what this session is working on
    recent_keywords: list[str] = field(default_factory=list)  # Keywords for routing
    posted_message_ts: list[str] = field(default_factory=list)  # Slack ts of messages posted by this session
    github_issue_id: int | None = None  # Optional linked GitHub issue
    active_thread_ts: str = ""  # Thread ts for replies routed via Tier 1 thread routing
    last_reported_error_hash: str = ""  # SHA256 of last reported API error (dedup)
    pool_name: str = ""  # Runtime pool this session belongs to (e.g., "claude", "codex")

    def is_stale(self, timeout: int) -> bool:
        """Check if the child has been idle longer than timeout seconds."""
        return (datetime.now() - self.last_message_at).total_seconds() > timeout


def window_name(config: HomeboundConfig, item_id: int, pool_name: str = "") -> str:
    """Generate tmux window name for a slot (e.g., CLAUDE-1, CODEX-2)."""
    pool = pool_name or config.default_pool
    return f"{config.pool_window_prefix(pool)}{item_id}"


def parse_window_name(wname: str, config: HomeboundConfig) -> tuple[int | None, str]:
    """Parse a slot number and pool name from a tmux window name.

    Returns (slot, pool_name) or (None, "") if not matching.
    """
    upper = wname.upper()
    for pool in config.pool_names:
        prefix = config.pool_window_prefix(pool)
        if upper.startswith(prefix):
            slot_text = wname[len(prefix):]
            try:
                slot = int(slot_text)
            except ValueError:
                continue
            if slot >= 1:
                return slot, pool
    return None, ""


def session_name(config: HomeboundConfig, item_id: int, pool_name: str = "") -> str:
    """Generate child agent name for transport identity."""
    pool = pool_name or config.default_pool
    return f"{config.pool_session_prefix(pool)}{item_id}"


def _item_label(config: HomeboundConfig, item_id: int, pool_name: str = "") -> str:
    """Format a user-facing item label (e.g., Claude1, Codex2, Agent1)."""
    pool = pool_name or config.default_pool
    return f"{config.pool_label(pool)}{item_id}"


_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "and", "but", "or",
    "not", "no", "nor", "so", "yet", "both", "either", "neither", "each",
    "every", "all", "any", "few", "more", "most", "other", "some", "such",
    "than", "too", "very", "just", "about", "up", "out", "if", "then",
    "that", "this", "these", "those", "it", "its", "i", "me", "my", "we",
    "our", "you", "your", "he", "him", "his", "she", "her", "they", "them",
    "their", "what", "which", "who", "whom", "when", "where", "why", "how",
    "work", "task", "issue", "please", "need", "want", "make", "get", "use",
})


def extract_keywords(text: str, max_keywords: int = 20) -> list[str]:
    """Extract meaningful keywords from text for routing heuristics."""
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        if w not in _STOPWORDS and w not in seen:
            seen.add(w)
            keywords.append(w)
            if len(keywords) >= max_keywords:
                break
    return keywords


def _sanitize_text(
    text: str,
    max_len: int,
    label: str = "",
    item_id: int = 0,
    item_label: str | None = None,
) -> str:
    """Strip newlines and truncate text for tmux safety."""
    clean = " ".join(text.splitlines())
    if len(clean) > max_len:
        if label:
            display_label = item_label or f"Session{item_id}"
            logger.warning(
                "%s: %s truncated from %d to %d chars",
                display_label, label, len(clean), max_len,
            )
        clean = clean[:max_len] + " ... [TRUNCATED — resend as shorter message]"
    return clean


async def spawn_child(
    item_id: int,
    task_text: str,
    config: HomeboundConfig,
    mode: str | None = None,
    pool_name: str = "",
) -> ChildInfo:
    """Spawn a new interactive CLI session in a tmux window.

    1. Creates a new tmux window named <prefix><N>
    2. Starts the CLI via the configured runtime
    3. Waits for initialization
    4. Sends the initial prompt based on mode

    Args:
        item_id: Issue/item number to work on.
        task_text: The task instructions from the transport.
        config: Homebound configuration.
        mode: Override mode (uses config.default_mode if None).
        pool_name: Runtime pool to use (empty = default runtime).

    Returns:
        ChildInfo tracking the new session.
    """
    if mode is None:
        mode = config.default_mode

    runtime = config.get_runtime(pool_name)
    tmux_session = config.tmux_session_name
    project_dir = config.project_dir
    label = _item_label(config, item_id, pool_name)

    wname = window_name(config, item_id, pool_name)
    target = f"{tmux_session}:{wname}"

    # Create new tmux window
    await new_window(tmux_session, wname)

    try:
        # Start the CLI via runtime adapter
        rc, _, err = await run_tmux(
            "send-keys", "-t", target,
            runtime.start_command(project_dir), "Enter",
        )
        if rc != 0:
            raise RuntimeError(f"Failed to start CLI in window {wname}: {err}")

        # Wait for CLI prompt
        prompt_ready = await wait_for_prompt(
            target,
            timeout=config.sessions.init_timeout,
            idle_markers=runtime.idle_prompt_markers(),
        )
        if not prompt_ready:
            raise RuntimeError(
                f"Timed out waiting for CLI prompt in window {wname} "
                f"after {config.sessions.init_timeout}s"
            )

        # Build the initial prompt based on mode
        initial_prompt = _build_prompt(item_id, task_text, mode, config, pool_name)
        logger.info("%s: using %s mode", label, mode)

        # Send the initial prompt
        if not await send_keys(target, initial_prompt):
            raise RuntimeError(f"Failed to send initial prompt to window {wname}")
    except Exception:
        # Clean up the tmux window so it doesn't become an orphan
        await kill_window(tmux_session, wname)
        raise

    child = ChildInfo(item_id=item_id, window_name=wname, pool_name=pool_name)
    logger.info("Spawned child session for %s in window %s", label, wname)
    return child


def _build_prompt(
    item_id: int,
    task_text: str,
    mode: str,
    config: HomeboundConfig,
    pool_name: str = "",
) -> str:
    """Build the initial prompt for a child session based on mode."""
    mode_config = config.modes.get(mode)
    label = _item_label(config, item_id, pool_name)
    if mode_config is None or not mode_config.prompt_template:
        # Fallback: use the task mode template
        return f"Work on item {label}. Read the full details and complete the task."

    # Prepare template variables
    sid = session_name(config, item_id, pool_name)
    post_command = config.transport.post_command_template.format(
        session_name=sid, message="your message", thread_ts="",
    )

    clean_text = _sanitize_text(
        task_text, config.sessions.max_message_len,
        label="task_text", item_id=item_id, item_label=label,
    )

    prompt = mode_config.prompt_template.format(
        item_id=item_id,
        work_item_label=label,
        task_text=clean_text,
        post_command=post_command,
        session_name=sid,
        name=config.name,
    )

    # Append standard progress-reporting instruction to all modes
    progress_suffix = (
        " PROGRESS UPDATES (mandatory): "
        "The user cannot see your terminal. Slack is your only communication channel. "
        "Post a brief progress update to Slack after each meaningful step "
        "(e.g. analysis done, implementation started, tests passing). "
        "Do not wait until the end — post as you go. "
        f"Slack command: {post_command}"
    )
    prompt += progress_suffix

    return prompt


async def send_to_child(
    child: ChildInfo,
    message: str,
    config: HomeboundConfig,
    thread_ts: str = "",
) -> None:
    """Route a follow-up message to an existing child session.

    Updates the child's last_message_at timestamp.
    """
    target = f"{config.tmux_session_name}:{child.window_name}"
    sid = session_name(config, child.item_id, child.pool_name)
    label = _item_label(config, child.item_id, child.pool_name)

    message = _sanitize_text(
        message, config.sessions.max_message_len,
        label="follow-up", item_id=child.item_id, item_label=label,
    )

    post_cmd = config.transport.post_command_template.format(
        session_name=sid, message="your status message", thread_ts=thread_ts,
    )
    wrapped = (
        f"Follow-up for {label}: "
        f"--- BEGIN TASK --- {message} --- END TASK --- "
        f"Format your Slack response with mrkdwn: *bold* headings, bullet lists, "
        f"`code` for values, and clear sections. "
        f"Use emojis sparingly — only in section headers or one-liner status lines, not in body text. "
        f"Post your results using: {post_cmd}"
    )

    success = await send_keys(target, wrapped)
    if success:
        child.last_message_at = datetime.now()
        logger.info("Routed message to %s", label)
    else:
        logger.error("Failed to route message to %s", label)


async def read_child_output(
    child: ChildInfo,
    config: HomeboundConfig,
    lines: int = 20,
) -> str:
    """Read recent output from a child's tmux pane."""
    target = f"{config.tmux_session_name}:{child.window_name}"
    return await capture_pane(target, lines=lines)


async def close_child(
    child: ChildInfo,
    config: HomeboundConfig,
) -> None:
    """Gracefully close a child session."""
    runtime = config.get_runtime(child.pool_name)
    target = f"{config.tmux_session_name}:{child.window_name}"

    # Send exit command
    await send_keys(target, runtime.exit_command())
    await asyncio.sleep(config.sessions.close_grace_period)

    # Kill the tmux window
    await kill_window(config.tmux_session_name, child.window_name)
    logger.info("Closed child session for %s", _item_label(config, child.item_id, child.pool_name))


async def adopt_child(
    item_id: int,
    config: HomeboundConfig,
    known_windows: list[str] | None = None,
    pool_name: str = "",
) -> ChildInfo:
    """Re-adopt an existing tmux window as a managed child.

    Used on restart to reclaim orphaned sessions.

    Args:
        item_id: The item ID to adopt.
        config: Homebound configuration.
        known_windows: Pre-fetched window list to avoid redundant tmux calls.
        pool_name: Runtime pool this session belongs to.
    """
    wname = window_name(config, item_id, pool_name)

    if known_windows is not None:
        windows = known_windows
    else:
        windows = await list_windows(config.tmux_session_name)

    label = _item_label(config, item_id, pool_name)
    if wname not in windows:
        raise RuntimeError(f"Cannot adopt {label}: window {wname} not found")
    child = ChildInfo(item_id=item_id, window_name=wname, pool_name=pool_name)
    logger.info("Adopted existing child session for %s", label)
    return child


async def verify_child_alive(
    child: ChildInfo,
    config: HomeboundConfig,
    window_set: set[str] | None = None,
) -> bool:
    """Check if a child's tmux window still exists.

    Args:
        child: The child to check.
        config: Homebound configuration.
        window_set: Pre-fetched set of window names to avoid redundant tmux calls.
    """
    if window_set is not None:
        return child.window_name in window_set
    windows = await list_windows(config.tmux_session_name)
    return child.window_name in windows


def list_custom_skills(project_dir: Path) -> list[tuple[str, str]]:
    """Scan .claude/skills/ for custom SKILL.md files.

    Checks both project-level (project_dir/.claude/skills/) and
    user-level (~/.claude/skills/) directories. Deduplicates by name,
    with project-level skills taking precedence.

    Returns list of (name, description) tuples sorted by name.
    """
    skills_dirs = [
        project_dir / ".claude" / "skills",
        Path.home() / ".claude" / "skills",
    ]
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for skills_dir in skills_dirs:
        if not skills_dir.is_dir():
            continue
        for entry in sorted(skills_dir.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if skill_file.is_symlink() or not skill_file.is_file():
                continue
            name, description = _parse_skill_frontmatter(skill_file)
            if name and name not in seen:
                seen.add(name)
                results.append((name, description))
    results.sort(key=lambda x: x[0])
    return results


def _parse_skill_frontmatter(path: Path) -> tuple[str, str]:
    """Parse YAML frontmatter from a SKILL.md file.

    Returns (name, description) or ("", "") if parsing fails.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ("", "")
    # Frontmatter must start with ---
    if not text.startswith("---"):
        return ("", "")
    end = text.find("\n---", 3)
    if end == -1:
        return ("", "")
    frontmatter = text[3:end]
    name = ""
    description = ""
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.lower().startswith("name:"):
            name = line[5:].strip().strip("\"'")
        elif line.lower().startswith("description:"):
            description = line[12:].strip().strip("\"'")
    return (name, description)
