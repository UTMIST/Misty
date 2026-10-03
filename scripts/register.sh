#!/usr/bin/env bash
# Register the Discord bot's slash commands against a Railway environment.
#
# You normally don't need this: discord-bot/railway.json runs registerCommands.js
# as the Railway preDeployCommand, so every deploy to staging or production
# registers that environment's bot. This wrapper is for re-registering WITHOUT a
# deploy (e.g. after changing DISCORD_GUILD_ID, or to clear stale guild
# commands). It's a thin wrapper over `railway run … node src/registerCommands.js`
# that can't silently skip staging (see #38, #39).
#
# Prereqs:
#   - `railway` CLI installed + authed (`railway login`), linked to the project
#   - Railway access to the `discord-bot` service in the target environment(s)
#
# Usage:
#   scripts/register.sh <staging|production|all>
#
#   all      → staging first, then production (with a confirmation prompt).
#   Secrets are injected by Railway; nothing prod-sensitive is needed locally.
set -euo pipefail

TARGET="${1:?usage: register.sh <staging|production|all>}"

# registerCommands.js is resolved relative to CWD, so run from discord-bot/.
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../discord-bot" && pwd)"
cd "$BOT_DIR"

command -v railway >/dev/null 2>&1 \
  || { echo "ERROR: railway CLI not found — install it and run 'railway login'." >&2; exit 1; }
railway whoami >/dev/null 2>&1 \
  || { echo "ERROR: not logged in to Railway — run 'railway login' first." >&2; exit 1; }

register() {
  local env="$1"
  echo "▶ registering slash commands ($env)…"
  railway run --service discord-bot --environment "$env" -- node src/registerCommands.js
  echo "✓ registered ($env)"
}

confirm_production() {
  read -r -p "About to register against PRODUCTION. Continue? [y/N] " reply
  case "$reply" in
    [yY] | [yY][eE][sS]) ;;
    *) echo "Aborted." >&2; exit 1 ;;
  esac
}

case "$TARGET" in
  staging)
    register staging
    ;;
  production)
    confirm_production
    register production
    ;;
  all)
    register staging
    confirm_production
    register production
    ;;
  *)
    echo "usage: register.sh <staging|production|all>" >&2
    exit 1
    ;;
esac
