# homebound

**Command your local AI agents from anywhere.**

## Demo

https://github.com/user-attachments/assets/e0f9f19b-ccb0-4715-b704-90fba7895937

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

https://github.com/user-attachments/assets/e0f9f19b-ccb0-4715-b704-90fba7895937

## Why Homebound?

Unlike cloud-hosted solutions, homebound runs agents on **your machine** with access to your real environment — databases, Docker, local services, git repos. No sandboxes. Your machine, your environment, your agents.

Sessions survive network drops and orchestrator restarts, and everything is observable: attach to any tmux window to see exactly what an agent is doing.

## Features

- **Smart routing** — with `ANTHROPIC_API_KEY` set and `llm_routing: true`, bare messages are automatically classified by Claude Haiku to the right session. No `@Agent1` prefixes needed — just type naturally and homebound figures out which agent should handle it.
- **Multi-model pools** — run Claude Code and Codex (or any CLI) simultaneously with named pools (`@Claude1`, `@Codex1`), sharing a single slot pool
- **Multi-session** — up to N concurrent agents in tmux, addressed via `@Agent` or `@Agent1` (configurable via `sessions.agent_label`)
- **Crash recovery** — state persistence (`children.json`), orphan re-adoption on restart
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
cd homebound-agents
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

> **Recommended:** Enable `llm_routing` for the best message routing. Without it, bare messages are routed via keyword matching (always on) and thread replies — keyword matching scores overlap between your message and each session's context, routing to the best match. With LLM routing also enabled, Claude Haiku adds semantic classification for ambiguous messages that keywords miss — so you can type "what about the auth bug?" and it routes to the right agent even without keyword overlap.

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

Homebound uses a pluggable adapter pattern:

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
- **Runtime** — Claude Code and Codex CLI built-in; other CLIs via `GenericCLIRuntime`
- **Tracker** — GitHub Issues built-in; extensible to Linear, Jira, etc.

See the [routing cascade and adapter details](docs/configuration.md#routing-cascade) for how messages reach the right agent.

## Configuration

All behavior is driven by `homebound.yaml`. Run `homebound init` to generate a complete config with all options and their defaults.

See the [Configuration Reference](docs/configuration.md#configuration-reference) for all fields, defaults, and multi-model pool setup.

## Known Limitations

- **Newline stripping** — messages sent to tmux have newlines stripped; multi-line code blocks may be affected
- **Single-channel** — each homebound instance monitors one Slack channel

## Development

```bash
git clone https://github.com/anoopy/homebound-agents
cd homebound-agents
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
pytest tests/ -v
```

See the [CLI Reference](docs/configuration.md#cli-reference) and [Project Structure](docs/configuration.md#project-structure) in the docs.

## Security Disclaimer

Homebound executes AI agent sessions with **full access to your local machine** — filesystem, shell, network, and any credentials in your environment. It is your responsibility to:

- Restrict access via `allowed_users` in `security` config
- Review agent actions (attach to any tmux window to observe)
- Run in environments where the blast radius is acceptable
- Never expose the Slack channel to untrusted users

This software is provided as-is for local development use. It is **not hardened for production or multi-tenant deployment**. The authors are not liable for any damage caused by agent actions.

## License

MIT — see [LICENSE](LICENSE) for details.
