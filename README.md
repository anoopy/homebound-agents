# homebound

**Command your local AI agents from anywhere.**

A Python orchestrator that lets you control AI coding agents running on your local machine via Slack. Send commands from anywhere, and homebound routes them to agent sessions managed in tmux — with smart routing, multi-model support, multi-agent parallelism, and crash recovery.

```
You (Slack)  ──→  Homebound (orchestrator)  ──→  Claude Code (tmux CLAUDE-1)
                         │                  ──→  Codex CLI   (tmux CODEX-2)
                         │                              │
                         ├── smart-routes messages      ├── works on GH issues
                         ├── manages session pool       ├── reads/writes code
                         ├── multi-model pools          └── posts results
                         └── reports status
```

## Why Homebound?

Unlike cloud-hosted solutions, homebound runs agents on **your machine** with access to your real environment — databases, Docker, local services, git repos. No sandboxes. Your machine, your environment, your agents.

Sessions survive network drops and orchestrator restarts, and everything is observable: attach to any tmux window to see exactly what an agent is doing.

## Features

- **Smart routing** — with `ANTHROPIC_API_KEY` set and `llm_routing: true`, bare messages are automatically classified by Claude Haiku to the right session. No `@Agent1` prefixes needed — just type naturally and homebound figures out which agent should handle it.
- **Multi-model pools** — run Claude Code and Codex (or any CLI) simultaneously with named pools (`@Claude1`, `@Codex1`), sharing a single slot pool
- **Multi-session** — up to N concurrent agents in tmux, addressed via `@Agent` or `@Agent1` (configurable via `sessions.agent_label`)
- **Crash recovery** — state persistence (`children.json`), orphan re-adoption on restart
- **GitHub integration** — list, create, view, and close issues directly from Slack
- **Security** — user allowlists, session ownership, destructive command confirmation
- **Prompt relay** — detects runtime prompts (e.g., CLI permission dialogs) and relays them to Slack for answers
- **Pluggable architecture** — swap transports, runtimes, and trackers via adapter ABCs
- **YAML configuration** — all behavior driven by a single `homebound.yaml`
- **Adaptive backoff** — exponential poll delay escalation during outages, automatic recovery notification
- **Chat mode** — flexible chat-based interaction with your agents

## Quick Start

### Prerequisites

- Python 3.10+ (`python3 --version` to check; on macOS with Homebrew: `brew install python`)
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (or any CLI agent)
- A Slack workspace where you can create apps
- [GitHub CLI](https://cli.github.com/) (`gh`) — for the built-in GitHub tracker
- `jq` and `curl` — used by `scripts/slack_post.sh` for agent Slack posts

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click **Create New App** → **From scratch**
2. Name it (e.g. "Homebound") and select your workspace
3. Navigate to **OAuth & Permissions** in the sidebar
4. Under **Bot Token Scopes**, add these scopes:
   - `chat:write` — post messages
   - `channels:history` — read channel messages
   - `channels:read` — list channels (for finding channel IDs)
5. Click **Install to Workspace** and authorize
6. Copy the **Bot User OAuth Token** (starts with `xoxb-`) — you'll need this in step 5

### 2. Create a Slack Channel

1. In Slack, create a new channel for homebound (e.g. `#homebound-agents`)
2. Invite your bot to the channel: type `/invite @Homebound` in the channel
3. Get the channel ID: right-click the channel name → **View channel details** → the ID is at the bottom (starts with `C`)

### 3. Install

```bash
git clone https://github.com/anoopy/homebound-agents
cd homebound
python3 -m venv venv   # Must be Python 3.10+; use python3.13 if python3 is older
source venv/bin/activate
pip install --upgrade pip
pip install -e ".[llm]"
```

> **Note:** `pip install --upgrade pip` is required — older pip versions (< 21.3) cannot install editable packages from `pyproject.toml`. The `[llm]` extra adds Claude Haiku for smart message routing (recommended). Use `pip install -e .` for the base package without LLM routing.

### 4. Generate config

```bash
homebound init
```

This creates a `homebound.yaml` with all available options and their defaults.

### 5. Configure

Edit `homebound.yaml` — paste your channel ID from step 2:

```yaml
orchestrator:
  name: "homebound"

transport:
  type: "slack"
  channel_id: "C07XXXXXXXX"  # Channel ID from step 2
  token_env: "SLACK_BOT_TOKEN"

tracker:
  type: "github"
  project_dir: "."  # Current directory; agents work in the repo you start from

# Single runtime (default):
runtime:
  type: "claude-code"
  command: "claude"

# Multi-runtime pools (optional — replaces `runtime:` above):
# runtimes:
#   claude:
#     type: "claude-code"
#     command: "claude --dangerously-skip-permissions"
#   codex:
#     type: "generic"
#     command: "codex --no-alt-screen --full-auto"
#     idle_markers: ["›"]

routing:
  llm_routing: true  # Recommended — requires ANTHROPIC_API_KEY
```

> **Recommended:** Enable `llm_routing` for significantly better message routing. Without it, bare messages (those without `@Agent` prefixes) can only be routed via thread replies or auto-spawned as new sessions. With LLM routing enabled, Claude Haiku classifies each unmatched message to the best active session based on context — so you can just type "what about the auth bug?" and it routes to the right agent automatically.

### 6. Set your tokens

Create a `.env` file in the repo root:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_CHANNEL_ID=C07XXXXXXXX
ANTHROPIC_API_KEY=sk-ant-...
```

`homeboundctl.sh` sources this file automatically and injects the tokens into the tmux environment. `SLACK_BOT_TOKEN` and `ANTHROPIC_API_KEY` are used by the orchestrator directly. `SLACK_CHANNEL_ID` is passed to child agent sessions so they can post results back to Slack via `scripts/slack_post.sh`. The script also inherits your shell's PATH from `~/.zshrc` or `~/.bash_profile`, so tools like `tmux`, `jq`, and `curl` are found even in non-interactive contexts.

### 7. Start

```bash
scripts/homeboundctl.sh start --config homebound.yaml
```

> **Note:** `--config` is resolved to an absolute path by the script, so relative paths work from the directory where you run the command.

### Example Slack commands

```text
@homebound help                              → list all commands
@homebound status                            → show active sessions
@Agent fix the login bug for #42             → spawn agent on issue #42
what about the auth middleware?               → smart-routed to the right agent (llm_routing: true)
@Agent1 close                                → close session 1

# Multi-model pools (when runtimes: is configured):
@Claude fix the login bug                    → spawn Claude Code session
@Codex refactor the utils module             → spawn Codex session
@Claude1 close                               → close specific Claude session
```

## Architecture

Homebound uses a pluggable adapter pattern. Each component is an abstract base class with concrete implementations:

```
HomeboundConfig (YAML) ──→ Orchestrator
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
          Transport        Tracker        Runtime
          (ABC)            (ABC)          (ABC)
              │                │              │
              ▼                ▼              ▼
        SlackTransport   GitHubTracker  ClaudeCodeRuntime
                                        GenericCLIRuntime
```

- **Transport** — Slack built-in; extensible to Discord, Telegram, etc.
- **Runtime** — Claude Code and Codex CLI built-in (tested); other CLIs via `GenericCLIRuntime`. Multiple runtimes can run simultaneously via named pools.
- **Tracker** — GitHub Issues built-in; extensible to Linear, Jira, etc.

### Routing cascade

When multiple agents are running, incoming messages are routed through this cascade:

```text
Incoming Slack message
  1. Thread reply?         (thread_ts maps to session)   → route to session
  2. Keyword match?        (keyword overlap scoring)     → route via keyword overlap
  3. LLM classification?   (llm_routing: true)           → Claude Haiku picks the best match
  4. Auto-spawn?           (if enabled)                  → spawn new session
```

Without LLM routing, step 2 (keyword matching) still runs before falling through to auto-spawn. With LLM routing enabled, Haiku reads the message along with recent conversation context (last 5 messages) and each session's keywords, then routes to the best match (~200-500ms per classification). This is the difference between "every bare message spawns a new agent" and "messages intelligently reach the right agent."

> **Note:** In single-runtime mode, agent names are configurable via `sessions.agent_label`. In multi-runtime mode, names derive from the pool keys in `runtimes:` (e.g., `claude` → `@Claude1`).

Adding a new adapter is straightforward: implement the ABC, add a `from_config` classmethod, and register it in the adapter registry.

## Configuration

Run `homebound init` to generate a complete starter config. Key sections:

| Section | Purpose |
|---------|---------|
| `orchestrator` | Name, aliases |
| `transport` | Slack channel, token, polling |
| `tracker` | GitHub project dir, admin pattern |
| `runtime` | Agent CLI command, idle markers (single-runtime mode) |
| `runtimes` | Named runtime pools for multi-model support (replaces `runtime`) |
| `sessions` | Max concurrent, timeouts, retries |
| `routing` | Thread routing, LLM routing, auto-spawn |
| `prompt_relay` | Detect and relay CLI prompts to Slack |
| `modes` | Chat prompt templates |
| `security` | User allowlists, destructive confirmation |

See the [Configuration Reference](#configuration-reference) below for all fields and defaults.

## Configuration Reference

All configuration lives in `homebound.yaml`. Run `homebound init` to generate a complete template.

### `orchestrator`

| Field | Default | Description |
|-------|---------|-------------|
| `name` | `"homebound"` | Orchestrator identity — used in Slack messages and tmux session name |
| `aliases` | `[]` | Short names that also trigger admin commands (e.g. `["hb"]`) |

### `transport`

| Field | Default | Description |
|-------|---------|-------------|
| `type` | `"slack"` | Transport adapter type |
| `channel_id` | `""` | Slack channel ID to monitor |
| `token_env` | `"SLACK_BOT_TOKEN"` | Environment variable holding the bot token |
| `lookback_minutes` | `5` | How far back to look for messages on each poll |
| `message_format` | `"*[{name}]* {message}"` | Format for outgoing messages |
| `strip_prefixes` | `["claude-desktop"]` | Prefixes to strip from incoming messages |
| `http_timeout` | `10` | HTTP request timeout in seconds |
| `poll_limit` | `20` | Max messages to fetch per poll cycle |

### `tracker`

| Field | Default | Description |
|-------|---------|-------------|
| `type` | `"github"` | Tracker adapter type |
| `project_dir` | `"."` | Path to the Git repository |
| `admin_pattern` | `^@{name}\s+(.+)` | Regex for admin commands |
| `command_timeout` | `30` | Timeout for `gh` CLI commands in seconds |

### `runtime`

| Field | Default | Description |
|-------|---------|-------------|
| `type` | `"claude-code"` | Runtime adapter type (`claude-code` or `generic`) |
| `command` | `"claude"` | Shell command to start the agent |
| `idle_markers` | `["❯", "> "]` | Strings that indicate the CLI is idle |
| `exit_command` | `"/exit"` | Command to gracefully exit the agent |

### `runtimes` (multi-model pools)

When present, `runtimes:` replaces the top-level `runtime:` key. Each entry defines a named pool with its own CLI backend. All pools share the same slot pool (`sessions.max_concurrent`).

```yaml
runtimes:
  claude:
    type: "claude-code"
    command: "claude --dangerously-skip-permissions"
    idle_markers: ["❯", "> "]
    exit_command: "/exit"
    env_unset: ["CLAUDECODE"]
  codex:
    type: "generic"
    command: "codex --no-alt-screen --full-auto"
    idle_markers: ["›"]
    exit_command: "/exit"
    env_unset: []
```

| Behavior | Single `runtime:` | Multi `runtimes:` |
|----------|-------------------|-------------------|
| Session labels | `Agent1`, `Agent2` | `Claude1`, `Codex2` |
| tmux windows | `AGENT-1` | `CLAUDE-1`, `CODEX-2` |
| Slack commands | `@Agent <task>` | `@Claude <task>`, `@Codex <task>` |
| Slot pool | Shared | Shared |
| Help text | Generic | Per-pool commands |

Pool names must be alphabetic. Each pool entry has the same fields as `runtime:` (`type`, `command`, `idle_markers`, `exit_command`, `env_unset`).

> **Codex note:** On first run, Codex shows a "trust this folder" prompt that must be accepted manually once via `scripts/homeboundctl.sh attach`. After that, Codex starts cleanly for all subsequent spawns.

### `sessions`

| Field | Default | Description |
|-------|---------|-------------|
| `agent_label` | `"Agent"` | User-facing name in single-runtime mode — labels become `Agent1`, windows `AGENT-1`, Slack commands `@Agent1`. Ignored when `runtimes:` is configured (pool names determine labels). |
| `max_concurrent` | `5` | Maximum parallel agent sessions |
| `idle_timeout` | `1800` | Seconds before idle detection (30 min) |
| `init_timeout` | `60` | Seconds to wait for agent prompt after spawn |
| `poll_interval` | `10` | Seconds between transport poll cycles |
| `max_retries` | `3` | Retry attempts for failed transport calls |
| `max_message_len` | `4000` | Max characters per message sent to tmux |
| `idle_warning_threshold` | `3` | Warnings before auto-closing idle session |
| `close_grace_period` | `2.0` | Seconds to wait after sending exit command |
| `outage_threshold` | `3` | Consecutive poll failures before adaptive backoff |
| `outage_max_interval` | `120` | Max poll interval during outage (seconds) |

### `routing`

| Field | Default | Description |
|-------|---------|-------------|
| `thread_routing` | `true` | Route thread replies to the originating session (uses `conversations.replies` polling) |
| `thread_poll_max_age` | `1800` | Max age in seconds for threads to poll (default 30 min) |
| `thread_poll_max_threads` | `10` | Max concurrent threads to poll per cycle |
| `keyword_routing` | `true` | Route via keyword overlap between message and session context |
| `keyword_match_threshold` | `2` | Minimum keyword overlap score to trigger routing |
| `llm_routing` | `false` | **Recommended.** Use Claude Haiku to classify bare messages to the best active session. Set to `true` and provide `ANTHROPIC_API_KEY` for significantly smarter routing. |
| `llm_model` | `"claude-haiku-4-5"` | Model used for LLM routing |
| `auto_spawn_on_no_match` | `true` | Auto-spawn a new session when no match found |
| `max_message_map_size` | `200` | Max entries in the thread-to-session map (pruned at 75%) |

### `security`

| Field | Default | Description |
|-------|---------|-------------|
| `allowed_users` | `[]` | Slack user IDs allowed to send commands |
| `destructive_confirm_timeout` | `60` | Seconds to confirm destructive commands |
| `allow_open_channel` | `false` | Allow any authenticated user when no allowlist is set |
| `allow_bots` | `false` | Allow bot/webhook messages |
| `allow_admin_takeover` | `false` | Allow admins to control other users' sessions |

### `prompt_relay`

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `true` | Detect and relay runtime prompts (e.g., permission dialogs) to Slack |
| `scan_lines` | `30` | Lines of tmux output to scan for prompts |
| `poll_every_cycles` | `1` | Scan frequency (every N poll cycles) |
| `option_patterns` | *(regex list)* | Patterns to detect option lines in CLI output |
| `question_mark_required` | `true` | Require `?` in question line for prompt detection |
| `ttl_seconds` | `300` | Pending prompt expiry (5 min) |
| `max_pending_per_issue` | `3` | Max pending prompts per session |

### `modes`

| Field | Default | Description |
|-------|---------|-------------|
| `default` | `"task"` | Default mode for new sessions |
| Per-mode `keyword` | `""` | Prefix keyword to trigger this mode (e.g., `"chat:"`) |
| Per-mode `prompt_template` | `""` | Template for the initial prompt sent to the agent |

## Known Limitations

- **Newline stripping** — messages sent to tmux have newlines stripped; multi-line code blocks may be affected
- **Single-channel** — each homebound instance monitors one Slack channel

## CLI Reference

```bash
homebound start [--config PATH] [--dry-run]   # Start the orchestrator
homebound stop [--config PATH]                 # Send shutdown signal
homebound status [--config PATH]               # Show tmux session windows
homebound init [--output PATH] [--force]       # Generate starter YAML config
```

### `homeboundctl.sh` — tmux session manager

```bash
scripts/homeboundctl.sh start [--config PATH]       # Start orchestrator in tmux
scripts/homeboundctl.sh start-dry [--config PATH]   # Start in dry-run mode
scripts/homeboundctl.sh stop [--config PATH]        # Stop orchestrator (children survive)
scripts/homeboundctl.sh stop-all [--config PATH]    # Stop everything
scripts/homeboundctl.sh status [--config PATH]      # Show tmux windows
scripts/homeboundctl.sh health [--config PATH]      # Full health report
scripts/homeboundctl.sh attach [--config PATH]      # Attach to the tmux session
scripts/homeboundctl.sh logs [--config PATH]        # Tail the log file
```

The script auto-sources `.env` from the repo root, injecting `SLACK_BOT_TOKEN` and `ANTHROPIC_API_KEY` into the tmux environment. If `--config` points to a missing file, it exits with an error instead of falling back to defaults.

## Development

```bash
git clone https://github.com/anoopy/homebound-agents
cd homebound
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
pytest tests/ -v
```

### Project structure

```
src/homebound/
  config.py              # YAML config loader + dataclasses
  orchestrator.py        # Main poll loop, session lifecycle
  routing.py             # RoutingEngine — thread, keyword, LLM routing
  prompt_relay.py        # PromptRelayManager — prompt detection and relay
  admin.py               # AdminCommandHandler — status, help, skills, tracker
  session.py             # Child session lifecycle, context enrichment
  tmux.py                # Async tmux wrappers
  cli.py                 # CLI entry point
  security.py            # Auth policy gate

  adapters/              # Abstract base classes
    runtime.py           # AgentRuntime ABC
    transport.py         # Transport ABC + IncomingMessage
    tracker.py           # Tracker ABC + ClassifiedCommand

  runtimes/              # Runtime implementations
    claude_code.py
    generic_cli.py

  transports/            # Transport implementations
    slack.py

  trackers/              # Tracker implementations
    github.py

tests/                   # 371 tests across all modules
scripts/
  homeboundctl.sh        # tmux session manager
  watchdog.sh            # Auto-restart watchdog
```

## Security Disclaimer

Homebound executes AI agent sessions with **full access to your local machine** — filesystem, shell, network, and any credentials in your environment. It is your responsibility to:

- Restrict access via `allowed_users` in `security` config
- Review agent actions (attach to any tmux window to observe)
- Run in environments where the blast radius is acceptable
- Never expose the Slack channel to untrusted users

This software is provided as-is for local development use. It is **not hardened for production or multi-tenant deployment**. The authors are not liable for any damage caused by agent actions.

## License

MIT — see [LICENSE](LICENSE) for details.
