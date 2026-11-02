"""The same search scenarios run against both storage adapters."""

from contracts.storage import StorageAdapter
from contracts.types import PersonCreate, PersonIdentifierCreate, PersonSearch, PersonUpdate


def check_person_search_contract(adapter: StorageAdapter) -> None:
    def create(name, email):
        return adapter.create_person(
            PersonCreate(display_name=name, primary_email=email), actor="t"
        )

    # Insert out of order; duplicate names must use stable UUID ordering.
    zoe = create("Zoe Chen", "zoe@example.org")
    alex = create("Alex Chen", "alex@example.org")
    other_alex = create("aLEX cHEN", "other@example.org")
    retired = create("Alex Retired", "retired@example.org")
    adapter.update_person(retired.id, PersonUpdate(active=False), actor="t")
    adapter.add_person_email(alex.id, "  ALTERNATE@Example.org  ", actor="t")
    # Matching primary + alternate on one person must not duplicate a result.
    adapter.add_person_email(alex.id, "alex@example.org", actor="t")
    adapter.add_person_email(alex.id, "second@example.org", actor="t")
    adapter.create_person_identifier(
        zoe.id,
        PersonIdentifierCreate(provider="uoft_email", external_id="legacy@example.org"),
        actor="t",
    )

    def search(**kwargs):
        return adapter.search_people(PersonSearch(**kwargs))

    def ids(result):
        return [match.id for match in result.matches]

    expected = sorted([alex.id, other_alex.id])
    result = search(name="  aLeX  ")
    assert ids(result) == expected
    assert result.needs_disambiguation is True
    assert result.has_more is False
    assert set(result.matches[0].model_dump()) == {"id", "display_name", "primary_email", "active"}
    assert search(name="aLeX").model_dump() == result.model_dump()

    result = search(name="chen", limit=1)
    assert ids(result) == expected[:1]
    assert result.has_more is True
    assert result.needs_disambiguation is True
    assert ids(search(name="chen", limit=2)) == expected
    assert search(name="chen", limit=2).has_more is True
    assert ids(search(name="chen", limit=3)) == [*expected, zoe.id]
    assert search(name="chen", limit=3).has_more is False

    for email in ["  ALEX@EXAMPLE.ORG ", "alternate@example.org", "second@example.org"]:
        result = search(email=email)
        assert ids(result) == [alex.id]
        assert result.has_more is False
        assert result.needs_disambiguation is False
        assert ids(search(name=" chen ", email=email)) == [alex.id]
        assert ids(search(name="Zoe", email=email)) == []
    assert ids(search(email="alex@")) == []  # exact email, not a substring
    assert ids(search(email="legacy@example.org")) == []  # only provider=email
    assert ids(search(name="nobody")) == []
    assert ids(search(email="nobody@example.org")) == []
    assert search(name="nobody").needs_disambiguation is False
    assert search(name="nobody").has_more is False
    assert ids(search(email="retired@example.org")) == []
    assert ids(search(name="retired")) == []
    assert ids(search(name="retired", active_only=False)) == [retired.id]
    assert search(email="retired@example.org", active_only=False).matches[0].active is False
    # Search returns unlinked people and does not create identifiers or reactivate.
    assert adapter.list_person_identifiers(other_alex.id) == []
    assert adapter.get_person(retired.id).active is False

    literal = create(r"100%_\\ Match", "literal@example.org")
    create("100 percent Match", "nonliteral@example.org")
    assert ids(search(name=r"%_\\")) == [literal.id]
    accented = create("Élise", "elise@example.org")
    assert ids(search(name="ÉLI")) == [accented.id]

    for index in range(11):
        create(f"Bounded {index:02}", f"bounded{index}@example.org")
    assert len(search(name="Bounded").matches) == 5
    result = search(name="Bounded", limit=10)
    assert len(result.matches) == 10
    assert result.matches[0].display_name == "Bounded 00"
    assert result.matches[-1].display_name == "Bounded 09"
    assert result.has_more is True

    # A primary address can conflict with an existing alternate after a normal
    # person edit/create. Search must return both candidates, never pick one.
    collision = create("Collision", "alternate@example.org")
    result = search(email="alternate@example.org", limit=1)
    assert ids(result) == [alex.id]
    assert result.has_more is True
    assert result.needs_disambiguation is True
    assert ids(search(email="alternate@example.org", name="collision")) == [collision.id]
