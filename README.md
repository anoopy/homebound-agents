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

- **Smart routing** — bare messages are automatically classified to the right session via Claude Haiku (`llm_routing: true`)
- **Multi-model pools** — run Claude Code and Codex (or any CLI) simultaneously (`@Claude1`, `@Codex1`)
- **Multi-session** — up to N concurrent agents in tmux
- **Crash recovery** — state persistence, orphan re-adoption on restart
- **GitHub integration** — list, create, view, and close issues from Slack
- **Prompt relay** — detects CLI permission dialogs and relays them to Slack
- **Pluggable architecture** — swap transports, runtimes, and trackers via adapter ABCs
- **YAML configuration** — all behavior driven by a single `homebound.yaml`

## Quick Start

### Prerequisites

- Python 3.10+, [tmux](https://github.com/tmux/tmux), [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), [GitHub CLI](https://cli.github.com/) (`gh`), `jq`, `curl`
- A Slack workspace with a bot app ([setup guide below](#1-create-a-slack-app))

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Under **OAuth & Permissions**, add scopes: `chat:write`, `channels:history`, `channels:read`
3. **Install to Workspace** and copy the **Bot User OAuth Token** (`xoxb-...`)

### 2. Create a Slack Channel

1. Create a channel (e.g. `#homebound-agents`), invite your bot (`/invite @Homebound`)
2. Get the channel ID from **View channel details** (starts with `C`)

### 3. Install

```bash
git clone https://github.com/youruser/homebound-agents
cd homebound-agents
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -e ".[llm]"
```

### 4. Configure

```bash
homebound init  # generates homebound.yaml with all options
```

Edit `homebound.yaml`:

```yaml
transport:
  channel_id: "C07XXXXXXXX"  # your channel ID

runtime:
  type: "claude-code"
  command: "claude"

# Optional: multi-model pools (replaces runtime: above)
# runtimes:
#   claude:
#     type: "claude-code"
#     command: "claude --dangerously-skip-permissions"
#   codex:
#     type: "generic"
#     command: "codex --no-alt-screen --full-auto"

routing:
  llm_routing: true  # recommended
```

Create a `.env` file:

```bash
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_CHANNEL_ID=C07XXXXXXXX
ANTHROPIC_API_KEY=sk-ant-...
```

### 5. Start

```bash
scripts/homeboundctl.sh start --config homebound.yaml
```

### Example Slack commands

```text
@homebound help                  → list commands
@homebound status                → show active sessions
@Agent fix the login bug         → spawn agent
what about the auth middleware?  → smart-routed (llm_routing: true)
@Agent1 close                    → close session

# Multi-model pools:
@Claude fix the login bug        → spawn Claude session
@Codex refactor utils            → spawn Codex session
```

## Known Limitations

- **Newline stripping** — messages sent to tmux have newlines stripped
- **Single-channel** — each instance monitors one Slack channel

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Reference

For full configuration options, architecture details, CLI commands, and project structure, see **[docs/configuration.md](docs/configuration.md)**.

## Security Disclaimer

Homebound executes AI agent sessions with **full access to your local machine**. Restrict access via `allowed_users`, review agent actions via tmux, and never expose the Slack channel to untrusted users. This software is provided as-is for local development use.

## License

MIT — see [LICENSE](LICENSE) for details.
