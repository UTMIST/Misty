from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from contracts.directory import DirectoryUnavailable
from contracts.fetcher import SourcePermission
from contracts.types import Source, SourceGrant
from contracts.visibility import Actor
from src.api.routers.docs import _source_access_due
from src.source_access import SourceAccessUnavailable, resolve_google_permissions
from src.storage.in_memory import InMemoryStorageAdapter

P1 = UUID("11111111-1111-1111-1111-111111111111")
P2 = UUID("22222222-2222-2222-2222-222222222222")
T1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class Directory:
    def __init__(self, *, people=None, groups=None, down=False):
        self.people = people or {}
        self.groups = groups or {}
        self.down = down

    def get_person_id_by_verified_email(self, email):
        if self.down:
            raise DirectoryUnavailable("down")
        return self.people.get(email)

    def get_team_id_by_synced_google_group(self, email):
        if self.down:
            raise DirectoryUnavailable("down")
        return self.groups.get(email)


def permission(permission_id, principal_type, principal=None, inherited_from=None):
    return SourcePermission(
        permission_id=permission_id,
        principal_type=principal_type,
        principal=principal,
        role="reader",
        inherited=bool(inherited_from),
        inherited_from=inherited_from or [],
    )


def test_direct_shares_resolve_each_verified_email_and_ignore_unknowns():
    resolved = resolve_google_permissions(
        [
            permission("a", "user", "one@example.com"),
            permission("b", "user", "two@example.com"),
            permission("c", "user", "unknown@example.com"),
        ],
        Directory(people={"one@example.com": P1, "two@example.com": P2}),
    )

    assert [(g.grantee_type, g.grantee_id) for g in resolved.grants] == [
        ("person", P1),
        ("person", P2),
    ]
    assert any("unknown@example.com" in warning for warning in resolved.warnings)


def test_mapped_synced_group_preserves_inherited_provenance():
    resolved = resolve_google_permissions(
        [permission("g1", "group", "team@example.com", ["folder", "shared-drive"])],
        Directory(groups={"team@example.com": T1}),
    )

    assert resolved.grants[0].grantee_type == "team"
    assert resolved.grants[0].grantee_id == T1
    assert resolved.grants[0].source_inherited_from == ["folder", "shared-drive"]
    assert resolved.grants[0].source_inherited is True


def test_source_permission_expiration_is_preserved():
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    resolved = resolve_google_permissions(
        [
            SourcePermission(
                permission_id="p1",
                principal_type="user",
                principal="one@example.com",
                role="reader",
                expiration_time=expires,
            )
        ],
        Directory(people={"one@example.com": P1}),
    )
    assert resolved.grants[0].source_expires_at == expires


def test_expired_source_permission_creates_no_grant():
    resolved = resolve_google_permissions(
        [
            SourcePermission(
                permission_id="expired",
                principal_type="user",
                principal="one@example.com",
                role="reader",
                expiration_time=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        ],
        Directory(people={"one@example.com": P1}),
    )
    assert resolved.grants == []
    assert resolved.warnings == ["expired source permission ignored: expired"]


def test_unmapped_groups_domain_and_anyone_never_create_grants():
    resolved = resolve_google_permissions(
        [
            permission("g", "group", "legacy@googlegroups.com"),
            permission("d", "domain", "example.com"),
            permission("a", "anyone"),
        ],
        Directory(),
    )

    assert resolved.grants == []
    assert len(resolved.warnings) == 3


def test_directory_failure_aborts_instead_of_returning_partial_acl():
    with pytest.raises(SourceAccessUnavailable):
        resolve_google_permissions(
            [permission("a", "user", "one@example.com")], Directory(down=True)
        )


def test_successful_resync_revokes_only_source_grants_and_expiry_fails_closed():
    now = datetime.now(timezone.utc)
    source = Source(
        id="gdocs",
        label="Google Docs",
        url_patterns=[],
        requires_auth=True,
        has_api=True,
        content_fetch_enabled=True,
        created_at=now,
        updated_at=now,
        created_by="system",
        updated_by="system",
    )
    store = InMemoryStorageAdapter(seed_sources=[source])
    doc = store.create_doc(
        url="https://docs.google.com/document/d/x/edit",
        url_normalized="https://docs.google.com/document/d/x/edit",
        source_id="gdocs",
        title="x",
        description=None,
        owning_team_id=None,
        owning_team_label=None,
        owning_person_id=None,
        owning_person_label=None,
        content_snapshot=None,
        fetched_at=None,
        tags=[],
        actor="test",
    )
    store.add_grant(doc.id, grantee_type="person", grantee_id=P2, actor="admin")
    store.replace_source_grants(
        doc.id,
        origin="google_drive",
        grants=[
            SourceGrant(
                grantee_type="person",
                grantee_id=P1,
                source_permission_id="p1",
                source_principal="one@example.com",
                source_role="reader",
            )
        ],
        synced_at=now,
        expires_at=now + timedelta(hours=1),
        actor="sync",
    )
    assert store.get_doc(doc.id, visibility=Actor(P1, frozenset())) is not None

    store.replace_source_grants(
        doc.id,
        origin="google_drive",
        grants=[],
        synced_at=now,
        expires_at=now + timedelta(hours=1),
        actor="sync",
    )
    assert store.get_doc(doc.id, visibility=Actor(P1, frozenset())) is None
    assert store.get_doc(doc.id, visibility=Actor(P2, frozenset())) is not None

    store.replace_source_grants(
        doc.id,
        origin="google_drive",
        grants=[
            SourceGrant(
                grantee_type="person",
                grantee_id=P1,
                source_permission_id="p2",
                source_principal="one@example.com",
                source_role="reader",
            )
        ],
        synced_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
        actor="sync",
    )
    assert store.get_doc(doc.id, visibility=Actor(P1, frozenset())) is None


def test_recent_failed_attempt_is_not_selected_again_in_same_batch_drain():
    now = datetime.now(timezone.utc)
    source = Source(
        id="gdocs",
        label="Google Docs",
        url_patterns=[],
        requires_auth=True,
        has_api=True,
        content_fetch_enabled=True,
        created_at=now,
        updated_at=now,
        created_by="system",
        updated_by="system",
    )
    store = InMemoryStorageAdapter(seed_sources=[source])
    doc = store.create_doc(
        url="https://docs.google.com/document/d/y/edit",
        url_normalized="https://docs.google.com/document/d/y/edit",
        source_id="gdocs",
        title="y",
        description=None,
        owning_team_id=None,
        owning_team_label=None,
        owning_person_id=None,
        owning_person_label=None,
        content_snapshot=None,
        fetched_at=None,
        tags=[],
        actor="test",
    )
    attempted = store.update_doc(
        doc.id,
        {
            "source_access_synced_at": now - timedelta(days=3),
            "source_access_attempted_at": now,
        },
        actor="scheduler",
    )
    assert _source_access_due(attempted, now - timedelta(days=1)) is False
