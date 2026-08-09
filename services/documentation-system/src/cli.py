"""documentation-system CLI — direct-to-DB API key management.

  doc-keys issue --name <name> --scopes <space-separated>
  doc-keys list [--active-only]
  doc-keys revoke <api_key_id>

`issue` PRINTS THE PLAINTEXT KEY ONCE to stdout. Capture it immediately."""

import argparse
import sys
from uuid import UUID

from sqlalchemy import create_engine

from src.api.hashing import generate_key
from src.config import get_settings


def _adapter():
    # Imported lazily: src.storage.postgres arrives in Task 15, and the CLI only
    # ever runs against a real DB, so importing here keeps src.cli importable now.
    from src.storage.postgres import PostgresStorageAdapter

    engine = create_engine(get_settings().database_url, future=True)
    return PostgresStorageAdapter(engine)


def cmd_issue(args: argparse.Namespace, adapter=None) -> int:
    adapter = adapter or _adapter()
    plaintext, prefix, key_hash = generate_key()
    key = adapter.create_api_key(
        name=args.name,
        prefix=prefix,
        key_hash=key_hash,
        scopes=list(args.scopes or []),
        actor=args.actor,
    )
    print("=" * 70, file=sys.stderr)
    print("API KEY ISSUED (only shown once)", file=sys.stderr)
    print(f"  Name:   {key.name}", file=sys.stderr)
    print(f"  Prefix: {key.prefix}", file=sys.stderr)
    print(f"  Scopes: {', '.join(key.scopes) if key.scopes else '(none)'}", file=sys.stderr)
    print(f"  Key id: {key.id}", file=sys.stderr)
    print(plaintext)
    return 0


def cmd_list(args: argparse.Namespace, adapter=None) -> int:
    adapter = adapter or _adapter()
    keys = adapter.list_api_keys(active_only=args.active_only)
    if not keys:
        print("(no keys)", file=sys.stderr)
        return 0
    for k in keys:
        active = "yes" if (k.active and k.revoked_at is None) else "no"
        scopes = ", ".join(k.scopes) if k.scopes else "(none)"
        print(f"{k.name:<25} {k.prefix:<10} {active:<7} {scopes}")
    return 0


def cmd_revoke(args: argparse.Namespace, adapter=None) -> int:
    adapter = adapter or _adapter()
    try:
        key_id = UUID(args.api_key_id)
    except ValueError:
        print(f"error: not a valid UUID: {args.api_key_id}", file=sys.stderr)
        return 2
    revoked = adapter.revoke_api_key(key_id, actor=args.actor)
    if revoked is None:
        print(f"error: no such api key: {key_id}", file=sys.stderr)
        return 1
    print(f"revoked {revoked.name} ({revoked.prefix})", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doc-keys", description="Manage documentation-system API keys."
    )
    p.add_argument("--actor", default="cli", help="Actor stamped on created_by/updated_by")
    subs = p.add_subparsers(dest="cmd", required=True)

    p_issue = subs.add_parser("issue", help="Issue a new API key")
    p_issue.add_argument("--name", required=True, help="Consumer name, e.g. 'discord-bot'")
    p_issue.add_argument(
        "--scopes",
        nargs="*",
        default=[],
        help="e.g. docs:read docs:write. Use 'admin' for wildcard.",
    )
    p_issue.set_defaults(func=cmd_issue)

    p_list = subs.add_parser("list", help="List keys (metadata only)")
    p_list.add_argument("--active-only", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_revoke = subs.add_parser("revoke", help="Revoke a key by id")
    p_revoke.add_argument("api_key_id")
    p_revoke.set_defaults(func=cmd_revoke)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
