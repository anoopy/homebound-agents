"""Tests for tmux helper behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch


def test_wait_for_prompt_detects_marker():
    from homebound.tmux import wait_for_prompt

    with (
        patch("homebound.tmux.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "homebound.tmux.run_tmux",
            new_callable=AsyncMock,
            return_value=(0, "booting\n> ready", ""),
        ),
    ):
        ready = asyncio.run(wait_for_prompt("homebound:CLAUDE-240", timeout=2, idle_markers=["> "]))
    assert ready is True


def test_wait_for_prompt_continues_when_output_active_without_marker():
    from homebound.tmux import wait_for_prompt

    with (
        patch("homebound.tmux.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "homebound.tmux.run_tmux",
            new_callable=AsyncMock,
            side_effect=[
                # Initial timeout (2 polls) — output active but no marker
                (0, "starting runtime...", ""),
                (0, "still starting...", ""),
                # Extended wait — prompt appears
                (0, "PROMPT>", ""),
            ],
        ),
    ):
        ready = asyncio.run(wait_for_prompt("homebound:CLAUDE-240", timeout=2, idle_markers=["PROMPT>"]))
    assert ready is True


def test_wait_for_prompt_fails_when_no_output_and_no_marker():
    from homebound.tmux import wait_for_prompt

    with (
        patch("homebound.tmux.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "homebound.tmux.run_tmux",
            new_callable=AsyncMock,
            side_effect=[
                (0, "", ""),
                (0, "", ""),
            ],
        ),
    ):
        ready = asyncio.run(wait_for_prompt("homebound:CLAUDE-240", timeout=2, idle_markers=["PROMPT>"]))
    assert ready is False


def test_wait_for_prompt_fails_after_repeated_capture_errors():
    from homebound.tmux import wait_for_prompt

    with (
        patch("homebound.tmux.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "homebound.tmux.run_tmux",
            new_callable=AsyncMock,
            side_effect=[
                (1, "", "err"),
                (1, "", "err"),
                (1, "", "err"),
            ],
        ),
    ):
        ready = asyncio.run(wait_for_prompt("homebound:CLAUDE-240", timeout=5, idle_markers=["PROMPT>"]))
    assert ready is False


def test_run_tmux_returns_error_on_timeout():
    """run_tmux should return error code on timeout, not hang indefinitely."""
    from homebound.tmux import run_tmux

    async def _hang_forever():
        await asyncio.sleep(999)
        return (b"", b"")

    with patch("homebound.tmux.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_proc = AsyncMock()
        mock_proc.communicate = _hang_forever
        mock_proc.kill = lambda: None  # kill() is synchronous
        mock_proc.wait = AsyncMock()
        mock_exec.return_value = mock_proc

        rc, stdout, stderr = asyncio.run(run_tmux("send-keys", "-t", "test", timeout=0.1))

    assert rc == 1
    assert "timeout" in stderr


def test_send_keys_returns_false_on_timeout():
    """send_keys should return False when tmux send-keys times out."""
    from homebound.tmux import send_keys

    with patch("homebound.tmux.run_tmux", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (1, "", "timeout after 60.0s")
        result = asyncio.run(send_keys("homebound:AGENT-1", "hello world"))

    assert result is False


def test_send_keys_cancels_copy_mode_before_sending():
    """send_keys should cancel copy mode before sending literal text."""
    from homebound.tmux import send_keys

    call_log = []

    async def _mock_run(*args, **kwargs):
        call_log.append(args)
        if "display-message" in args:
            return (0, "1", "")  # pane is in copy mode
        return (0, "", "")

    with (
        patch("homebound.tmux.run_tmux", side_effect=_mock_run),
        patch("homebound.tmux.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = asyncio.run(send_keys("homebound:AGENT-1", "hello"))

    assert result is True
    # Should have: display-message (check), send-keys -X cancel, send-keys -l, send-keys Enter
    commands = [args[0] for args in call_log]
    assert "display-message" in commands
    assert any("-X" in args and "cancel" in args for args in call_log)


def test_send_keys_skips_cancel_when_not_in_copy_mode():
    """send_keys should not cancel copy mode when pane is in normal mode."""
    from homebound.tmux import send_keys

    call_log = []

    async def _mock_run(*args, **kwargs):
        call_log.append(args)
        if "display-message" in args:
            return (0, "0", "")  # pane is NOT in copy mode
        return (0, "", "")

    with (
        patch("homebound.tmux.run_tmux", side_effect=_mock_run),
        patch("homebound.tmux.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = asyncio.run(send_keys("homebound:AGENT-1", "hello"))

    assert result is True
    # Should NOT have sent -X cancel
    assert not any("-X" in args and "cancel" in args for args in call_log)


def test_wait_for_prompt_fails_when_extended_wait_exhausted():
    """Extended wait should return False when output seen but no marker after 2x timeout."""
    from homebound.tmux import wait_for_prompt

    with (
        patch("homebound.tmux.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "homebound.tmux.run_tmux",
            new_callable=AsyncMock,
            # All polls return output but never a prompt marker
            side_effect=[
                (0, "starting runtime...", ""),
                (0, "still starting...", ""),
                # Extended wait (2 more polls for timeout=2 → extended to 4)
                (0, "loading modules...", ""),
                (0, "still loading...", ""),
            ],
        ),
    ):
        ready = asyncio.run(wait_for_prompt("homebound:CLAUDE-240", timeout=2, idle_markers=["PROMPT>"]))
    assert ready is False
