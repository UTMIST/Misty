import pytest

from contracts.types import TeamCreate, TeamUpdate


def check_github_mapping_contract(store):
    a = store.create_team(TeamCreate(slug="github_a", label="A"), actor="test")
    b = store.create_team(TeamCreate(slug="github_b", label="B"), actor="test")
    assert store.get_github_mapping(a.id) is None
    assert store.reserve_github_mapping(a.id, 123, actor="admin").status == "pending"
    with pytest.raises(ValueError, match="mapping_exists"):
        store.reserve_github_mapping(a.id, 123, actor="admin")
    mapped = store.finish_github_mapping(a.id, 123, 77, "old", "Old", actor="admin")
    renamed = store.finish_github_mapping(a.id, 123, 77, "new", "New", actor="admin")
    assert renamed.github_team_id == mapped.github_team_id
    assert renamed.version > mapped.version
    store.reserve_github_mapping(b.id, 123, actor="admin")
    with pytest.raises(ValueError, match="github_team_already_mapped"):
        store.finish_github_mapping(b.id, 123, 77, "new", "New", actor="admin")
    with pytest.raises(ValueError, match="identity_change_forbidden"):
        store.finish_github_mapping(a.id, 123, 88, "other", "Other", actor="admin")
    store.update_team(a.id, TeamUpdate(active=False), actor="admin")
    assert store.get_github_mapping(a.id).status == "inactive"
    store.update_team(a.id, TeamUpdate(active=True), actor="admin")
    assert store.get_github_mapping(a.id).status == "inactive"
    assert store.deactivate_github_mapping(b.id, actor="admin").status == "inactive"

    retry = store.create_team(TeamCreate(slug="retry", label="Retry"), actor="test")
    first = store.reserve_github_mapping(retry.id, 123, actor="first")
    store.mark_github_mapping_retryable(retry.id, first.version, actor="first")
    released = store.get_github_mapping(retry.id)
    assert released.status == "retryable"
    with pytest.raises(ValueError, match="inactive_or_unknown_mapping"):
        store.finish_github_mapping(retry.id, 123, 99, "retry", "Retry", actor="stale")
    second = store.reserve_github_mapping(retry.id, 123, actor="second")
    assert second.status == "pending" and second.version > released.version
    assert second.created_by == "first" and second.updated_by == "second"
    store.mark_github_mapping_retryable(retry.id, first.version, actor="stale")
    assert store.get_github_mapping(retry.id).status == "pending"
    with pytest.raises(ValueError, match="mapping_exists"):
        store.reserve_github_mapping(retry.id, 123, actor="concurrent")
    store.finish_github_mapping(retry.id, 123, 99, "retry", "Retry", actor="second")
    store.mark_github_mapping_retryable(retry.id, second.version, actor="stale")
    assert store.get_github_mapping(retry.id).status == "active"
