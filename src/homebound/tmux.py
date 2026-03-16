"""Low-level tmux wrapper for session and window management.

Provides async helpers for creating windows, sending keys, capturing
pane output, and other tmux operations used by the orchestrator and
session manager.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("homebound.tmux")


async def run_tmux(*args: str) -> tuple[int, str, str]:
    """Run a tmux command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else 0,
        stdout.decode().strip(),
        stderr.decode().strip(),
    )


async def wait_for_prompt(
    target: str,
    timeout: int = 20,
    idle_markers: list[str] | None = None,
) -> bool:
    """Poll tmux capture-pane until a prompt marker appears.

    Args:
        target: tmux target (e.g. "session:window").
        timeout: Max seconds to wait before giving up.
        idle_markers: Strings that indicate the CLI is idle.

    Returns:
        True if prompt was detected, False if timed out.
    """
    markers = idle_markers if idle_markers is not None else ["\u276f", "> "]
    elapsed = 0
    saw_output = False
    consecutive_failures = 0
    while elapsed < timeout:
        await asyncio.sleep(1)
        elapsed += 1
        rc, stdout, _ = await run_tmux(
            "capture-pane", "-t", target, "-p", "-S", "-5",
        )
        if rc != 0:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                logger.error(
                    "capture-pane failing repeatedly for %s — window may be dead",
                    target,
                )
                return False
            continue
        consecutive_failures = 0
        if stdout.strip():
            saw_output = True
        last_lines = stdout.strip().splitlines()[-3:] if stdout.strip() else []
        if any(
            any(marker in line for marker in markers)
            for line in last_lines
        ):
            logger.info("Prompt detected for %s after %ds", target, elapsed)
            return True

    if saw_output:
        # Agent is starting up but prompt hasn't appeared yet — extend the wait.
        logger.info(
            "Prompt marker not detected for %s after %ds, but output is active — extending wait",
            target, timeout,
        )
        while elapsed < timeout * 2:
            await asyncio.sleep(1)
            elapsed += 1
            rc, stdout, _ = await run_tmux(
                "capture-pane", "-t", target, "-p", "-S", "-5",
            )
            if rc != 0:
                continue
            last_lines = stdout.strip().splitlines()[-3:] if stdout.strip() else []
            if any(
                any(marker in line for marker in markers)
                for line in last_lines
            ):
                logger.info("Prompt detected for %s after %ds (extended wait)", target, elapsed)
                return True
        logger.warning(
            "Prompt marker not detected for %s after %ds (extended) — continuing anyway",
            target, elapsed,
        )
        return True

    logger.warning("Prompt not detected for %s after %ds", target, timeout)
    return False


async def send_keys(target: str, message: str) -> bool:
    """Send a message to a tmux target via send-keys.

    Uses tmux literal flag (-l) to avoid interpreting special characters,
    then sends Enter separately.

    Args:
        target: tmux target (e.g. "session:window").
        message: Text to type into the session.

    Returns:
        True if both send-keys calls succeeded.
    """
    rc, _, err = await run_tmux(
        "send-keys", "-t", target, "-l", message,
    )
    if rc != 0:
        logger.error("send-keys failed for %s: %s", target, err)
        return False

    # Brief pause to let the CLI finish processing the bracketed paste
    await asyncio.sleep(0.3)

    rc2, _, err2 = await run_tmux("send-keys", "-t", target, "Enter")
    if rc2 != 0:
        logger.error("send-keys Enter failed for %s: %s", target, err2)
        return False

    return True


async def capture_pane(target: str, lines: int = 20) -> str:
    """Read recent output from a tmux pane.

    Args:
        target: tmux target.
        lines: Number of lines to capture (from bottom).

    Returns:
        The captured text.
    """
    rc, stdout, err = await run_tmux(
        "capture-pane", "-t", target, "-p", "-S", f"-{lines}",
    )
    if rc != 0:
        logger.error("capture-pane failed for %s: %s", target, err)
        return ""
    return stdout


async def list_windows(tmux_session: str) -> list[str]:
    """List all tmux windows in a session.

    Returns:
        List of window names.
    """
    rc, stdout, _ = await run_tmux(
        "list-windows", "-t", tmux_session, "-F", "#{window_name}",
    )
    if rc != 0 or not stdout:
        return []
    return stdout.splitlines()


async def new_window(tmux_session: str, window_name: str) -> None:
    """Create a new tmux window in the given session.

    Raises:
        RuntimeError: If window creation fails.
    """
    rc, _, err = await run_tmux(
        "new-window", "-t", tmux_session, "-n", window_name,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to create tmux window {window_name}: {err}")


async def kill_window(tmux_session: str, window_name: str) -> None:
    """Kill a tmux window."""
    target = f"{tmux_session}:{window_name}"
    rc, _, err = await run_tmux("kill-window", "-t", target)
    if rc != 0:
        logger.warning(
            "kill-window failed for %s (may already be closed): %s",
            target, err,
        )
