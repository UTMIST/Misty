from contracts.types import PersonCreate, PersonIdentifierCreate


def check_github_identity_contract(store):
    import pytest

    person = store.create_person(
        PersonCreate(display_name="One", primary_email="one@utmist.ca"), actor="test"
    )
    other = store.create_person(
        PersonCreate(display_name="Two", primary_email="two@utmist.ca"), actor="test"
    )
    store.create_person_identifier(
        person.id, PersonIdentifierCreate(provider="discord", external_id="123"), actor="test"
    )
    store.create_person_identifier(
        other.id, PersonIdentifierCreate(provider="discord", external_id="456"), actor="test"
    )
    store.create_person_identifier(
        person.id,
        PersonIdentifierCreate(provider="github", external_id="old-unverified"),
        actor="test",
    )
    assert store.get_verified_github(person.id) is None
    store.set_verified_github(person.id, "123", 77, "first", actor="connector")
    assert (
        store.set_verified_github(person.id, "123", 77, "renamed", actor="connector").login
        == "renamed"
    )
    with pytest.raises(ValueError, match="already_linked"):
        store.set_verified_github(other.id, "456", 77, "renamed", actor="connector")
    with pytest.raises(ValueError, match="unlink_before"):
        store.set_verified_github(person.id, "123", 88, "different", actor="connector")
    with pytest.raises(ValueError, match="binding_changed"):
        store.set_verified_github(person.id, "456", 77, "renamed", actor="connector")
    store.unlink_verified_github(person.id, actor="connector")
    assert store.get_verified_github(person.id) is None
    assert (
        store.set_verified_github(other.id, "456", 77, "renamed", actor="connector").github_id == 77
    )

    store.unlink_verified_github(other.id, actor="connector")
    assert (
        store.set_verified_github(person.id, "123", 88, "switched", actor="connector").github_id
        == 88
    )
