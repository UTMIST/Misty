from src import cli
from src.api.hashing import parse_prefix, verify_key
from src.storage.in_memory import InMemoryStorageAdapter


def test_issue_mints_verifiable_key(monkeypatch, capsys):
    store = InMemoryStorageAdapter()
    monkeypatch.setattr(cli, "_adapter", lambda: store)
    rc = cli.main(["issue", "--name", "gh-action", "--scopes", "resolve:discord"])
    assert rc == 0
    plaintext = capsys.readouterr().out.strip()
    prefix = parse_prefix(plaintext)
    assert store.get_api_key_hash(prefix) is not None
    assert verify_key(plaintext, store.get_api_key_hash(prefix)) is True
    assert store.get_api_key_by_prefix(prefix).scopes == ["resolve:discord"]
