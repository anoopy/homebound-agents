#!/bin/bash
# Post a message to Slack from an agent session.
#
# Usage:
#   slack_post.sh --name <session-name> --post "message"
#   echo "message" | slack_post.sh --name <session-name>    # stdin mode (avoids shell quoting issues)
#
# Requires: SLACK_BOT_TOKEN and SLACK_CHANNEL_ID in environment
#   (both are injected by homeboundctl.sh into the tmux session)

set -euo pipefail

NAME=""
MESSAGE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)  NAME="$2";    shift 2 ;;
    --post)  MESSAGE="$2"; shift 2 ;;
    *)       echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# Read from stdin if --post was not provided and stdin is not a terminal
if [ -z "$MESSAGE" ] && [ ! -t 0 ]; then
  MESSAGE="$(cat)"
fi

if [ -z "$MESSAGE" ]; then
  echo "Usage: slack_post.sh --name <name> --post \"message\"" >&2
  echo "   or: echo \"message\" | slack_post.sh --name <name>" >&2
  exit 1
fi

: "${SLACK_BOT_TOKEN:?SLACK_BOT_TOKEN is not set}"
: "${SLACK_CHANNEL_ID:?SLACK_CHANNEL_ID is not set}"

# Prefix with session name if provided (format: Claude1 instead of claude-1)
if [ -n "$NAME" ]; then
  # Capitalize first letter and remove hyphen for display: claude-1 → Claude1
  DISPLAY_NAME="$(echo "$NAME" | awk '{sub(/-/,""); print toupper(substr($0,1,1)) substr($0,2)}')"
  TEXT="*[${DISPLAY_NAME}]*
${MESSAGE}"
else
  TEXT="$MESSAGE"
fi

curl -s -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg channel "$SLACK_CHANNEL_ID" --arg text "$TEXT" \
    '{channel: $channel, text: $text, blocks: [{type: "section", text: {type: "mrkdwn", text: $text}}]}')" \
  > /dev/null
