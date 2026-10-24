"""Scheduled-run completion is separate from recorded Google reconciliation status."""

import json
from datetime import date, timedelta

import pytest

from conftest import build_seed_providers, build_seed_role_kinds
from contracts.groups import GroupProviderError
from contracts.types import TeamUpdate
from src import groups_cli
from src.google_groups import sync_team, synced_team_for_group
from src.storage.in_memory import InMemoryStorageAdapter
from test_google_groups import FakeGroups, _member, _team


@pytest.fixture
def storage():
    return InMemoryStorageAdapter(
        seed_role_kinds=build_seed_role_kinds(), seed_providers=build_seed_providers()
    )


@pytest.fixture
def cli(monkeypatch, storage):
    groups = FakeGroups()
    monkeypatch.setattr(groups_cli, "build_group_provider", lambda settings: groups)
    monkeypatch.setattr(groups_cli, "create_engine", lambda *args, **kwargs: None)
    monkeypatch.setattr(groups_cli, "PostgresStorageAdapter", lambda engine: storage)
    return groups


def _summary(capsys):
    return json.loads(capsys.readouterr().out.splitlines()[-1])


def test_rejected_member_is_recorded_without_stopping_other_work(storage, cli, monkeypatch, capsys):
    team = _team(storage, "first")
    existing = _member(storage, team, "existing@example.com")
    departed = _member(
        storage, team, "departed@example.com", started_at=date.today() - timedelta(days=2)
    )
    previous = sync_team(storage, cli, team.id, actor="setup")
    membership = storage.list_memberships(person_id=departed.id)[0]
    storage.end_membership(membership.id, date.today(), actor="test")
    storage.add_person_email(existing.id, "rejected@example.com", actor="test")
    _member(storage, team, "zaccepted@example.com")
    other = _team(storage, "second")
    _member(storage, other, "other@example.com")
    add = cli.add_member

    def reject_one(group, email):
        if email == "rejected@example.com":
            raise GroupProviderError("HTTP 403: member rejected")
        add(group, email)

    monkeypatch.setattr(cli, "add_member", reject_one)

    assert groups_cli.main(["sync"]) == 0

    failed = storage.get_team_google_group(team.id)
    assert failed.status == "failed"
    assert "403" in failed.last_error
    assert failed.last_synced_at == previous.last_synced_at
    assert failed.pending_changes == []
    assert set(cli.members[failed.group_name]) == {"existing@example.com", "zaccepted@example.com"}
    assert synced_team_for_group(storage, failed.group_email) is None
    assert storage.get_team_google_group(other.id).status == "synced"
    summary = _summary(capsys)
    assert summary == {
        "event": "groups_sync_finish",
        "level": "warning",
        "outcome": "incomplete",
        "synced_teams": 1,
        "incomplete_teams": 1,
        "skipped_teams": 0,
        "exit_code": 0,
    }


def test_completed_and_retired_teams_are_reported(storage, cli, capsys):
    team = _team(storage, "active")
    _member(storage, team, "member@example.com")
    retired = _team(storage, "retired")
    storage.update_team(retired.id, TeamUpdate(active=False), actor="test")

    assert groups_cli.main(["sync"]) == 0

    summary = _summary(capsys)
    assert summary["outcome"] == "synced"
    assert summary["synced_teams"] == 1
    assert summary["skipped_teams"] == 1
    assert storage.get_team_google_group(retired.id) is None


def test_unconfigured_provider_still_fails(cli, monkeypatch, capsys):
    monkeypatch.setattr(groups_cli, "build_group_provider", lambda settings: None)

    assert groups_cli.main(["sync"]) == 2
    captured = capsys.readouterr()
    assert "not configured" in captured.err
    assert "groups_sync_finish" not in captured.out


def test_unknown_team_still_fails(cli, capsys):
    assert groups_cli.main(["sync", "--team", "missing"]) == 2
    assert "no team" in capsys.readouterr().err


def test_unexpected_failure_is_not_converted_to_success(storage, cli, monkeypatch, capsys):
    _team(storage)

    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(groups_cli, "sync_team", unavailable)

    with pytest.raises(RuntimeError, match="database unavailable"):
        groups_cli.main(["sync"])
    assert "groups_sync_finish" not in capsys.readouterr().out
