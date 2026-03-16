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
            return_value=(0, "booting\nready > ", ""),
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
