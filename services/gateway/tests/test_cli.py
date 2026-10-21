import pytest

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


def test_issue_reports_a_duplicate_name_without_a_traceback(monkeypatch, capsys):
    store = InMemoryStorageAdapter()
    monkeypatch.setattr(cli, "_adapter", lambda: store)
    args = ["issue", "--name", "gh-action", "--scopes", "resolve:discord"]
    assert cli.main(args) == 0
    capsys.readouterr()

    assert cli.main(args) == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    # Nothing on stdout: a caller piping stdout to a secrets store must not be
    # handed a key that was never persisted.
    assert captured.out.strip() == ""


@pytest.mark.parametrize("scopes", [[], ["--scopes"], ["--scopes", "admin"], ["--scopes", "typo"]])
def test_issue_rejects_missing_or_unknown_scopes_before_storage(monkeypatch, scopes):
    def unexpected_store():
        pytest.fail("invalid permissions must fail before connecting to storage")

    monkeypatch.setattr(cli, "_adapter", unexpected_store)
    with pytest.raises(SystemExit) as exc:
        cli.main(["issue", "--name", "consumer", *scopes])
    assert exc.value.code == 2


def test_list_exposes_id_for_revoke_but_no_key_material(monkeypatch, capsys):
    store = InMemoryStorageAdapter()
    monkeypatch.setattr(cli, "_adapter", lambda: store)
    cli.main(["issue", "--name", "consumer", "--scopes", "resolve:discord"])
    plaintext = capsys.readouterr().out.strip()
    key = store.list_api_keys()[0]
    cli.main(["list", "--active-only"])
    output = capsys.readouterr().out
    assert str(key.id) in output
    assert plaintext not in output
    assert store.get_api_key_hash(key.prefix) not in output
    assert cli.main(["revoke", str(key.id)]) == 0
    assert store.list_api_keys(active_only=True) == []


def test_list_exposes_creation_and_revocation_audit_metadata(monkeypatch, capsys):
    store = InMemoryStorageAdapter()
    monkeypatch.setattr(cli, "_adapter", lambda: store)
    cli.main(["--actor", "issuer", "issue", "--name", "consumer", "--scopes", "resolve:discord"])
    capsys.readouterr()
    key = store.list_api_keys()[0]
    cli.main(["--actor", "revoker", "revoke", str(key.id)])
    capsys.readouterr()
    cli.main(["list"])
    output = capsys.readouterr().out
    revoked = store.list_api_keys()[0]
    assert f"created_at={key.created_at.isoformat()}" in output
    assert "created_by=issuer" in output
    assert f"updated_at={revoked.updated_at.isoformat()}" in output
    assert "updated_by=revoker" in output
