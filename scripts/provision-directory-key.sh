#!/usr/bin/env bash
# Mint scoped team-tracking API keys for the directory consumers and wire them
# onto the Railway services as DIRECTORY_API_KEY. Run once per environment.
#
# Prereqs:
#   - `railway` CLI installed + authed (`railway login`), linked to the project
#   - `uv` + the team-tracking project available locally
#   - TT_DATABASE_URL = the team-tracking Neon branch DATABASE_URL for this env
#
# Usage:
#   TT_DATABASE_URL="postgresql+psycopg://...neon.../team_tracking" \
#     scripts/provision-directory-key.sh <staging|production>
#
# NOTE: the exact `railway variables` flags vary by CLI version — if it errors,
# run `railway variables --help` and adjust the two set commands below.
set -euo pipefail

ENVIRONMENT="${1:?usage: provision-directory-key.sh <staging|production>}"
: "${TT_DATABASE_URL:?set TT_DATABASE_URL to the team-tracking Neon branch for $ENVIRONMENT}"

TT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../services/team-tracking" && pwd)"

# Issue a scoped key against the team-tracking Neon branch and echo the tt_... token.
issue_key() {
  local name="$1"; shift
  DATABASE_URL="$TT_DATABASE_URL" uv --project "$TT_DIR" run team-tracking-keys issue \
    --name "$name" --scopes "$@" \
    | grep -oE 'tt_[A-Za-z0-9_-]+' | head -n1
}

echo "▶ discord-bot: issuing scoped directory key ($ENVIRONMENT)…"
BOT_KEY="$(issue_key "discord-bot-$ENVIRONMENT" \
  people:read people:write people:elevate identifiers:read identifiers:write \
  teams:read teams:write memberships:read memberships:write role_kinds:read)" \
  || { echo "ERROR: failed to mint discord-bot key" >&2; exit 1; }
[ -n "$BOT_KEY" ] || { echo "ERROR: discord-bot key came back empty" >&2; exit 1; }
railway variables --service discord-bot --environment "$ENVIRONMENT" --set "DIRECTORY_API_KEY=$BOT_KEY"

echo "▶ documentation-system: issuing scoped directory key ($ENVIRONMENT)…"
DOCS_KEY="$(issue_key "documentation-system-$ENVIRONMENT" \
  people:read teams:read memberships:read identifiers:read)" \
  || { echo "ERROR: failed to mint documentation-system key" >&2; exit 1; }
[ -n "$DOCS_KEY" ] || { echo "ERROR: documentation-system key came back empty" >&2; exit 1; }
railway variables --service documentation-system --environment "$ENVIRONMENT" --set "DIRECTORY_API_KEY=$DOCS_KEY"

echo "✓ provisioned DIRECTORY_API_KEY for discord-bot + documentation-system in $ENVIRONMENT"
