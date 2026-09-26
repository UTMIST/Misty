"""Mint an `llm_` API key for an internal consumer.

Prints the plaintext key ONCE to stdout (give it to the consumer) and the
CONSUMER_KEYS JSON entry to stderr (add it to the service's config). No DB.

USAGE: uv run llm-keys --name <consumer> [--scopes chat embed ...]
"""

import argparse
import json
import sys

from src.api.hashing import generate_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-keys", description="Mint an internal LLM API key.")
    parser.add_argument("--name", required=True, help="Consumer label, e.g. 'reviewer-summaries'")
    parser.add_argument(
        "--scopes",
        nargs="*",
        default=[],
        help=(
            "Scopes (default: none). 'chat' allows POST /chat, 'embed' allows "
            "POST /embed. Neither implies the other — grant both if the consumer "
            "needs both. 'admin' is a wildcard over all scopes."
        ),
    )
    args = parser.parse_args(argv)

    plaintext, prefix, key_hash = generate_key()
    entry = {"name": args.name, "prefix": prefix, "key_hash": key_hash, "scopes": list(args.scopes)}

    print("=" * 70, file=sys.stderr)
    print("API KEY (give to the consumer — shown only once):", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    print(plaintext)  # stdout, pipe-friendly
    print("", file=sys.stderr)
    print("Append this object to the CONSUMER_KEYS JSON array:", file=sys.stderr)
    print(json.dumps(entry), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
