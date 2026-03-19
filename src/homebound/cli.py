"""Homebound CLI — entry point for `homebound` command.

Commands:
    homebound start [--config PATH] [--dry-run]
    homebound stop
    homebound status
    homebound init
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_dir: Path) -> None:
    """Configure logging to both file and stderr."""
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        log_dir / "homebound.log", maxBytes=5_000_000, backupCount=3,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    root = logging.getLogger("homebound")
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(sh)


def cmd_start(args: argparse.Namespace) -> None:
    """Start the orchestrator."""
    from homebound.config import load_config
    from homebound.orchestrator import Orchestrator

    config = load_config(args.config)

    log_dir = config.project_dir / "tmp" / config.name
    setup_logging(log_dir)

    logger = logging.getLogger("homebound")

    # Check required transport token
    token_env = config.transport.token_env
    if not os.environ.get(token_env):
        logger.error("%s not set. Add it to your environment and restart.", token_env)
        sys.exit(1)

    orchestrator = Orchestrator(config=config, dry_run=args.dry_run)

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGINT, orchestrator.request_shutdown)
    loop.add_signal_handler(signal.SIGTERM, orchestrator.request_shutdown)

    try:
        loop.run_until_complete(orchestrator.run())
    finally:
        loop.close()


def cmd_init(args: argparse.Namespace) -> None:
    """Generate a starter homebound.yaml from current defaults."""
    from homebound.config import HomeboundConfig

    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"{output} already exists. Use --force to overwrite.")
        sys.exit(1)

    # All values below are drawn from HomeboundConfig defaults.
    # This ensures the template always matches the actual defaults.
    d = HomeboundConfig()

    # Format list fields for YAML
    def yaml_list(items: list) -> str:
        return "[" + ", ".join(f'"{i}"' for i in items) + "]"

    def yaml_block_list(items: list) -> str:
        """Block-style list with single quotes — safe for regex patterns."""
        return "\n".join(f"    - '{i}'" for i in items)

    template = f"""\
# homebound.yaml — Configuration for Homebound orchestrator
# Docs: https://github.com/youruser/homebound-agents

orchestrator:
  name: "{d.orchestrator.name}"
  # aliases: ["hb"]  # Short names that also trigger admin commands

transport:
  type: "{d.transport.type}"
  channel_id: ""  # Your Slack channel ID
  token_env: "{d.transport.token_env}"
  lookback_minutes: {d.transport.lookback_minutes}
  message_format: "{d.transport.message_format}"
  strip_prefixes: {yaml_list(d.transport.strip_prefixes)}
  http_timeout: {d.transport.http_timeout}
  poll_limit: {d.transport.poll_limit}

tracker:
  type: "{d.tracker.type}"
  project_dir: "."  # Replace with your project path
  admin_pattern: '^@{{name}}\\s+(.+)'
  command_timeout: {d.tracker.command_timeout}

runtimes:
  agent:
    type: "claude-code"
    command: "claude"
    idle_markers: ["\u276f", "> "]
    exit_command: "/exit"
    env_unset: ["CLAUDECODE"]
  # Add more pools to run multiple AI backends simultaneously:
  # codex:
  #   type: generic
  #   command: "codex --no-alt-screen --full-auto"
  #   idle_markers: ["›"]

sessions:
  max_concurrent: {d.sessions.max_concurrent}
  idle_timeout: {d.sessions.idle_timeout}
  init_timeout: {d.sessions.init_timeout}
  poll_interval: {d.sessions.poll_interval}
  max_message_len: {d.sessions.max_message_len}
  idle_warning_threshold: {d.sessions.idle_warning_threshold}
  close_grace_period: {d.sessions.close_grace_period}
  max_retries: {d.sessions.max_retries}

prompt_relay:
  enabled: {str(d.prompt_relay.enabled).lower()}
  scan_lines: {d.prompt_relay.scan_lines}
  poll_every_cycles: {d.prompt_relay.poll_every_cycles}
  option_patterns:
{yaml_block_list(d.prompt_relay.option_patterns)}
  question_mark_required: {str(d.prompt_relay.question_mark_required).lower()}
  ttl_seconds: {d.prompt_relay.ttl_seconds}
  max_pending_per_issue: {d.prompt_relay.max_pending_per_issue}

routing:
  thread_routing: {str(d.routing.thread_routing).lower()}
  keyword_routing: {str(d.routing.keyword_routing).lower()}
  llm_routing: {str(d.routing.llm_routing).lower()}  # Recommended — requires ANTHROPIC_API_KEY
  auto_spawn_on_no_match: {str(d.routing.auto_spawn_on_no_match).lower()}

modes:
  default: "{d.default_mode}"
  task:
    prompt_template: "{d.modes['task'].prompt_template}"
  freeform:
    keyword: "{d.modes['freeform'].keyword}"
    prompt_template: "{d.modes['freeform'].prompt_template}"
  chat:
    keyword: "{d.modes['chat'].keyword}"
    prompt_template: "{d.modes['chat'].prompt_template}"

security:
  allowed_users: []
  destructive_confirm_timeout: {d.security.destructive_confirm_timeout}
  allow_open_channel: true
  allow_bots: {str(d.security.allow_bots).lower()}
  allow_admin_takeover: {str(d.security.allow_admin_takeover).lower()}

close_commands: {yaml_list(sorted(d.close_commands))}
"""
    output.write_text(template)
    print(f"Created {output}")


def _tmux_session_running(session: str) -> bool:
    """Check if a tmux session exists."""
    import subprocess
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def cmd_status(args: argparse.Namespace) -> None:
    """Show orchestrator status."""
    import subprocess

    from homebound.config import load_config

    config = load_config(args.config)
    session = config.tmux_session_name

    if not _tmux_session_running(session):
        print(f"Homebound not running (session '{session}').")
        return

    print(f"Homebound session '{session}' windows:")
    subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F",
         "  #{window_index}: #{window_name} (#{window_activity_string})"],
    )


def cmd_stop(args: argparse.Namespace) -> None:
    """Stop the orchestrator."""
    import subprocess

    from homebound.config import load_config

    config = load_config(args.config)
    session = config.tmux_session_name

    if not _tmux_session_running(session):
        print(f"Homebound not running (session '{session}').")
        return

    # Send Ctrl-C to orchestrator window
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}:orchestrator", "C-c"],
        capture_output=True,
    )
    print(f"Sent shutdown signal to '{session}'.")


def main():
    parser = argparse.ArgumentParser(
        description="Homebound — command your local AI agents from anywhere",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # start
    p_start = subparsers.add_parser("start", help="Start the orchestrator")
    p_start.add_argument("--config", type=str, default=None, help="Path to homebound.yaml")
    p_start.add_argument("--dry-run", action="store_true", help="Log actions without spawning children")

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop the orchestrator")
    p_stop.add_argument("--config", type=str, default=None, help="Path to homebound.yaml")

    # status
    p_status = subparsers.add_parser("status", help="Show orchestrator status")
    p_status.add_argument("--config", type=str, default=None, help="Path to homebound.yaml")

    # init
    p_init = subparsers.add_parser("init", help="Generate starter homebound.yaml")
    p_init.add_argument("--output", type=str, default="homebound.yaml", help="Output file path")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing file")

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "init":
        cmd_init(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
