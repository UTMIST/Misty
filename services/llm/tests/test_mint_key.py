import json

from platform_auth import key_store_from_config

from src.api.hashing import parse_prefix, verify_key
from src.mint_key import main


def test_mint_prints_key_and_config_entry(capsys):
    rc = main(["--name", "reviewer-summaries", "--scopes", "chat"])
    assert rc == 0
    out = capsys.readouterr()

    plaintext = out.out.strip()  # plaintext key on stdout
    assert plaintext.startswith("llm_")

    # The JSON entry is printed to stderr; round-trip it into a store.
    entry_line = out.err.strip().splitlines()[-1]
    entry = json.loads(entry_line)
    assert entry["name"] == "reviewer-summaries"
    assert entry["scopes"] == ["chat"]

    store = key_store_from_config(json.dumps([entry]))
    prefix = parse_prefix(plaintext)
    assert prefix == entry["prefix"]
    assert verify_key(plaintext, store.get_api_key_hash(prefix)) is True
