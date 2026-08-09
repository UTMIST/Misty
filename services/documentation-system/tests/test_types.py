import pytest
from pydantic import ValidationError

from contracts.types import Doc, DocIngest, Source
from datetime import datetime, timezone
from uuid import uuid4


def _audit():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {"created_at": now, "updated_at": now, "created_by": "system", "updated_by": "system"}


def test_source_requires_capabilities():
    s = Source(
        id="web",
        label="Web page",
        url_patterns=[],
        requires_auth=False,
        has_api=False,
        content_fetch_enabled=True,
        **_audit(),
    )
    assert s.id == "web"
    assert s.content_fetch_enabled is True


def test_doc_defaults_tags_empty_and_active_true():
    d = Doc(
        id=uuid4(),
        url="https://x.com",
        url_normalized="https://x.com",
        title=None,
        source_id="web",
        description=None,
        owning_team_id=None,
        owning_team_label=None,
        owning_person_id=None,
        owning_person_label=None,
        content_snapshot=None,
        fetched_at=None,
        **_audit(),
    )
    assert d.tags == []
    assert d.active is True


def test_docingest_only_url_required():
    p = DocIngest(url="https://x.com")
    assert p.source_id is None
    assert p.tags == []


def test_docingest_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        DocIngest(url="https://x.com", bogus="nope")


def test_doc_grants_table_shape():
    from src.storage.schema import doc_grants

    cols = set(doc_grants.c.keys())
    assert cols == {"id", "doc_id", "grantee_type", "grantee_id", "created_at", "created_by"}
    constraint_names = {c.name for c in doc_grants.constraints if c.name}
    assert "ck_doc_grants_grantee_shape" in constraint_names
    assert "uq_doc_grants_grantee" in constraint_names


def test_doc_grant_input_org_rejects_id():
    from contracts.types import DocGrantInput

    with pytest.raises(ValidationError):
        DocGrantInput(grantee_type="org", grantee_id="11111111-1111-1111-1111-111111111111")


def test_doc_grant_input_person_requires_id():
    from contracts.types import DocGrantInput

    with pytest.raises(ValidationError):
        DocGrantInput(grantee_type="person", grantee_id=None)


def test_doc_grant_input_valid_shapes():
    from contracts.types import DocGrantInput

    assert DocGrantInput(grantee_type="org").grantee_id is None
    g = DocGrantInput(grantee_type="team", grantee_id="11111111-1111-1111-1111-111111111111")
    assert str(g.grantee_id) == "11111111-1111-1111-1111-111111111111"
