"""gateway-keys — direct-to-DB management of the gateway's EXTERNAL API keys.

  gateway-keys issue --name <name> --scopes resolve:discord
  gateway-keys list [--active-only]
  gateway-keys revoke <api_key_id>

`issue` PRINTS THE PLAINTEXT KEY ONCE to stdout; the argon2 hash is all that's stored.
"""

import argparse
import sys
from uuid import UUID

from sqlalchemy import create_engine

from src.api.hashing import generate_key
from src.config import get_settings
from src.storage.postgres import PostgresStorageAdapter


def _adapter() -> PostgresStorageAdapter:
    return PostgresStorageAdapter(create_engine(get_settings().database_url, future=True))


def cmd_issue(args: argparse.Namespace) -> int:
    plaintext, prefix, key_hash = generate_key()
    key = _adapter().create_api_key(
        name=args.name, prefix=prefix, key_hash=key_hash, scopes=list(args.scopes or []),
        actor=args.actor,
    )
    print("=" * 70, file=sys.stderr)
    print("EXTERNAL API KEY ISSUED (shown once)", file=sys.stderr)
    print(f"  Name:   {key.name}", file=sys.stderr)
    print(f"  Scopes: {', '.join(key.scopes) if key.scopes else '(none)'}", file=sys.stderr)
    print(f"  Key id: {key.id}", file=sys.stderr)
    print(plaintext)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    keys = _adapter().list_api_keys(active_only=args.active_only)
    if not keys:
        print("(no keys)", file=sys.stderr)
        return 0
    for k in keys:
        active = "yes" if (k.active and k.revoked_at is None) else "no"
        print(f"{k.name:<25} {k.prefix:<10} {active:<7} {', '.join(k.scopes) or '(none)'}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    try:
        key_id = UUID(args.api_key_id)
    except ValueError:
        print(f"error: not a valid UUID: {args.api_key_id}", file=sys.stderr)
        return 2
    revoked = _adapter().revoke_api_key(key_id, actor=args.actor)
    if revoked is None:
        print(f"error: no such api key: {key_id}", file=sys.stderr)
        return 1
    print(f"revoked {revoked.name} ({revoked.prefix})", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gateway-keys", description="Manage gateway external API keys.")
    p.add_argument("--actor", default="cli")
    subs = p.add_subparsers(dest="cmd", required=True)
    pi = subs.add_parser("issue")
    pi.add_argument("--name", required=True)
    pi.add_argument("--scopes", nargs="*", default=[])
    pi.set_defaults(func=cmd_issue)
    pl = subs.add_parser("list")
    pl.add_argument("--active-only", action="store_true")
    pl.set_defaults(func=cmd_list)
    pr = subs.add_parser("revoke")
    pr.add_argument("api_key_id")
    pr.set_defaults(func=cmd_revoke)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
