"""team-tracking-groups — create and reconcile every team's Google Group.

Backfills groups for existing teams, retries failed syncs, and applies
date-based membership changes (a started_at/ended_at that has since passed)
that no API write triggers. Run it once after enabling Google Groups, then on
a schedule (see docs/DEPLOYMENT.md).

USAGE:
  team-tracking-groups sync [--team <slug>]

Exits 1 if any team is not `synced`.
"""

import argparse
import sys

from sqlalchemy import create_engine

from src.api.deps import build_group_provider
from src.config import get_settings
from src.google_groups import sync_team
from src.storage.postgres import PostgresStorageAdapter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="team-tracking-groups")
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="create missing groups and reconcile members")
    sync.add_argument("--team", help="only this team slug")
    sync.add_argument("--actor", default="cli:team-tracking-groups")
    args = parser.parse_args(argv)

    settings = get_settings()
    groups = build_group_provider(settings)
    if groups is None:
        print("ERROR: Google Groups is not configured (see .env.example).", file=sys.stderr)
        return 2
    storage = PostgresStorageAdapter(create_engine(settings.database_url, future=True))

    if args.team:
        team = storage.get_team_by_slug(args.team)
        if team is None:
            print(f"ERROR: no team with slug {args.team!r}", file=sys.stderr)
            return 2
        teams = [team]
    else:
        teams = storage.list_teams()

    ok = True
    for team in teams:
        state = sync_team(storage, groups, team.id, actor=args.actor)
        if state is None:
            print(f"{team.slug:<25} skipped (retired, no group)")
            continue
        print(f"{team.slug:<25} {state.status:<24} {state.group_email} {state.last_error or ''}")
        ok = ok and state.status == "synced"
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
