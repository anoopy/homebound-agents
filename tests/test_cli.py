"""Tests for CLI helpers."""

from __future__ import annotations

import argparse


def test_cmd_init_emits_usable_open_channel_security_block(tmp_path):
    from homebound.cli import cmd_init

    output = tmp_path / "homebound.yaml"
    args = argparse.Namespace(output=str(output), force=False)

    cmd_init(args)

    text = output.read_text()
    assert "allowed_users: []" in text
    assert "allow_open_channel: true" in text
