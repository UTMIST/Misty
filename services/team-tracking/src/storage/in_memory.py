from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from contracts.types import (
    ApiKey,
    Person,
    PersonCreate,
    PersonIdentifier,
    PersonIdentifierCreate,
    PersonIdentifierUpdate,
    PersonUpdate,
    Provider,
    RoleKind,
    Team,
    TeamCreate,
    TeamMembership,
    TeamMembershipCreate,
    TeamMembershipUpdate,
    TeamUpdate,
)
from src.storage.errors import UnknownParentTeamError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_email(v: str) -> str:
    return v.strip().lower()


def _ranges_overlap(start1: date, end1: date | None, start2: date, end2: date | None) -> bool:
    """Half-open [start, end) overlap; end=None means +infinity.

    Mirrors the Postgres EXCLUDE constraint on
    daterange(started_at, COALESCE(ended_at, 'infinity')) with && (overlap).
    Empty ranges (start >= end) overlap nothing, matching daterange semantics.
    """
    if end1 is not None and start1 >= end1:
        return False
    if end2 is not None and start2 >= end2:
        return False
    lo1_before_hi2 = end2 is None or start1 < end2
    lo2_before_hi1 = end1 is None or start2 < end1
    return lo1_before_hi2 and lo2_before_hi1


class InMemoryStorageAdapter:
    """In-process storage adapter used in tests and for quick prototyping.

    Not thread-safe. Not persistent. Enforces the same semantic invariants as
    the Postgres adapter (email uniqueness, slug uniqueness, no schema surprises).
    """

    def is_ready(self) -> bool:
        return True

    def __init__(
        self,
        seed_role_kinds: list[RoleKind] | None = None,
        seed_providers: list[Provider] | None = None,
    ) -> None:
        self._people: dict[UUID, Person] = {}
        self._teams: dict[UUID, Team] = {}
        self._role_kinds: dict[str, RoleKind] = {rk.id: rk for rk in (seed_role_kinds or [])}
        self._memberships: dict[UUID, TeamMembership] = {}
        self._api_keys: dict[UUID, ApiKey] = {}
        self._api_key_hashes: dict[UUID, str] = {}
        self._providers: dict[str, Provider] = {pr.id: pr for pr in (seed_providers or [])}
        self._identifiers: dict[UUID, PersonIdentifier] = {}

    # --- People ---

    def create_person(self, payload: PersonCreate, *, actor: str) -> Person:
        email = payload.primary_email.strip().lower()
        if any(p.primary_email == email for p in self._people.values()):
            raise ValueError(f"primary_email already exists: {email}")
        now = _now()
        p = Person(
            id=uuid4(),
            display_name=payload.display_name,
            primary_email=email,
            active=True,
            access_level=payload.access_level,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._people[p.id] = p
        return p

    def get_person(self, person_id: UUID) -> Person | None:
        return self._people.get(person_id)

    def get_person_by_email(self, primary_email: str) -> Person | None:
        target = primary_email.strip().lower()
        for p in self._people.values():
            if p.primary_email == target:
                return p
        return None

    def list_people(self, *, active_only: bool = False) -> list[Person]:
        people = list(self._people.values())
        if active_only:
            people = [p for p in people if p.active]
        return people

    def update_person(self, person_id: UUID, payload: PersonUpdate, *, actor: str) -> Person | None:
        existing = self._people.get(person_id)
        if existing is None:
            return None
        data = existing.model_dump()
        patch = payload.model_dump(exclude_unset=True)
        if "primary_email" in patch:
            patch["primary_email"] = _norm_email(patch["primary_email"])
        if "primary_email" in patch and patch["primary_email"] != existing.primary_email:
            new_email = patch["primary_email"]
            if any(
                p.primary_email == new_email and p.id != person_id for p in self._people.values()
            ):
                raise ValueError(f"primary_email already exists: {new_email}")
        data.update(patch)
        data["updated_at"] = _now()
        data["updated_by"] = actor
        updated = Person(**data)
        self._people[person_id] = updated
        return updated

    # --- Teams ---

    def create_team(self, payload: TeamCreate, *, actor: str) -> Team:
        if any(t.slug == payload.slug for t in self._teams.values()):
            raise ValueError(f"slug already exists: {payload.slug}")
        if payload.parent_id is not None and payload.parent_id not in self._teams:
            raise UnknownParentTeamError(payload.parent_id)
        now = _now()
        t = Team(
            id=uuid4(),
            slug=payload.slug,
            label=payload.label,
            description=payload.description,
            parent_id=payload.parent_id,
            active=True,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._teams[t.id] = t
        return t

    def get_team(self, team_id: UUID) -> Team | None:
        return self._teams.get(team_id)

    def get_team_by_slug(self, slug: str) -> Team | None:
        for t in self._teams.values():
            if t.slug == slug:
                return t
        return None

    def list_teams(self, *, active_only: bool = False) -> list[Team]:
        teams = list(self._teams.values())
        if active_only:
            teams = [t for t in teams if t.active]
        return teams

    def update_team(self, team_id: UUID, payload: TeamUpdate, *, actor: str) -> Team | None:
        existing = self._teams.get(team_id)
        if existing is None:
            return None
        data = existing.model_dump()
        patch = payload.model_dump(exclude_unset=True)
        if "slug" in patch and patch["slug"] != existing.slug:
            if any(t.slug == patch["slug"] and t.id != team_id for t in self._teams.values()):
                raise ValueError(f"slug already exists: {patch['slug']}")
        data.update(patch)
        data["updated_at"] = _now()
        data["updated_by"] = actor
        updated = Team(**data)
        self._teams[team_id] = updated
        return updated

    # --- Role kinds ---

    def get_role_kind(self, role_kind_id: str) -> RoleKind | None:
        return self._role_kinds.get(role_kind_id)

    def list_role_kinds(self, *, active_only: bool = False) -> list[RoleKind]:
        kinds = list(self._role_kinds.values())
        if active_only:
            kinds = [k for k in kinds if k.active]
        return kinds

    # --- Team memberships ---

    def create_membership(self, payload: TeamMembershipCreate, *, actor: str) -> TeamMembership:
        if payload.person_id not in self._people:
            raise ValueError(f"person_id not found: {payload.person_id}")
        if payload.team_id not in self._teams:
            raise ValueError(f"team_id not found: {payload.team_id}")
        if payload.role_kind_id not in self._role_kinds:
            raise ValueError(f"role_kind_id not found: {payload.role_kind_id}")
        new_start = payload.started_at or date.today()
        new_end = payload.ended_at
        for existing in self._memberships.values():
            if existing.person_id != payload.person_id or existing.team_id != payload.team_id:
                continue
            if _ranges_overlap(new_start, new_end, existing.started_at, existing.ended_at):
                raise ValueError(
                    "membership overlaps an existing active membership for this person and team"
                )
        now = _now()
        m = TeamMembership(
            id=uuid4(),
            person_id=payload.person_id,
            team_id=payload.team_id,
            role_kind_id=payload.role_kind_id,
            is_team_admin=payload.is_team_admin,
            started_at=payload.started_at or date.today(),
            ended_at=payload.ended_at,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._memberships[m.id] = m
        return m

    def get_membership(self, membership_id: UUID) -> TeamMembership | None:
        return self._memberships.get(membership_id)

    def list_memberships(
        self,
        *,
        team_id: UUID | None = None,
        person_id: UUID | None = None,
        active_only: bool = False,
        as_of: date | None = None,
        is_team_admin: bool | None = None,
    ) -> list[TeamMembership]:
        results = list(self._memberships.values())
        if team_id is not None:
            results = [m for m in results if m.team_id == team_id]
        if person_id is not None:
            results = [m for m in results if m.person_id == person_id]
        if active_only:
            # "Currently active" = open-ended OR ends strictly after today, so a
            # future-dated ended_at still counts as active now.
            today = date.today()
            results = [m for m in results if m.ended_at is None or m.ended_at > today]
        if as_of is not None:
            results = [
                m
                for m in results
                if m.started_at <= as_of and (m.ended_at is None or m.ended_at > as_of)
            ]
        if is_team_admin is not None:
            results = [m for m in results if m.is_team_admin == is_team_admin]
        return results

    def update_membership(
        self, membership_id: UUID, payload: TeamMembershipUpdate, *, actor: str
    ) -> TeamMembership | None:
        existing = self._memberships.get(membership_id)
        if existing is None:
            return None
        data = existing.model_dump()
        patch = payload.model_dump(exclude_unset=True)
        if "role_kind_id" in patch and patch["role_kind_id"] not in self._role_kinds:
            raise ValueError(f"role_kind_id not found: {patch['role_kind_id']}")
        data.update(patch)
        data["updated_at"] = _now()
        data["updated_by"] = actor
        updated = TeamMembership(**data)
        # Mirror the Postgres EXCLUDE constraint: the updated range must not
        # overlap any OTHER active membership for the same (person, team). A
        # PATCH that re-opens/extends ended_at can re-introduce an overlap, so
        # re-check here, excluding the row being updated from the comparison.
        for other in self._memberships.values():
            if other.id == membership_id:
                continue
            if other.person_id != updated.person_id or other.team_id != updated.team_id:
                continue
            if _ranges_overlap(
                updated.started_at, updated.ended_at, other.started_at, other.ended_at
            ):
                raise ValueError(
                    "membership overlaps an existing active membership for this person and team"
                )
        self._memberships[membership_id] = updated
        return updated

    def end_membership(
        self, membership_id: UUID, ended_at: date, *, actor: str
    ) -> TeamMembership | None:
        """Set ended_at only.

        Delegates to update_membership; correctness relies on
        TeamMembershipUpdate.model_dump(exclude_unset=True) skipping fields
        the caller didn't set. If TeamMembershipUpdate ever grows a field
        with a non-None default, revisit this method.
        """
        return self.update_membership(
            membership_id, TeamMembershipUpdate(ended_at=ended_at), actor=actor
        )

    # --- API keys ---

    def create_api_key(
        self,
        *,
        name: str,
        prefix: str,
        key_hash: str,
        scopes: list[str],
        actor: str,
    ) -> ApiKey:
        if any(k.name == name for k in self._api_keys.values()):
            raise ValueError(f"name already exists: {name}")
        if any(k.prefix == prefix for k in self._api_keys.values()):
            raise ValueError(f"prefix already exists: {prefix}")
        now = _now()
        key = ApiKey(
            id=uuid4(),
            name=name,
            prefix=prefix,
            scopes=scopes,
            active=True,
            revoked_at=None,
            last_used_at=None,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._api_keys[key.id] = key
        self._api_key_hashes[key.id] = key_hash
        return key

    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        for k in self._api_keys.values():
            if k.prefix == prefix:
                return k
        return None

    def get_api_key_hash(self, prefix: str) -> str | None:
        for k in self._api_keys.values():
            if k.prefix == prefix and k.active and k.revoked_at is None:
                return self._api_key_hashes.get(k.id)
        return None

    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]:
        keys = list(self._api_keys.values())
        if active_only:
            keys = [k for k in keys if k.active and k.revoked_at is None]
        return keys

    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None:
        existing = self._api_keys.get(api_key_id)
        if existing is None:
            return None
        now = _now()
        data = existing.model_dump()
        data["active"] = False
        data["revoked_at"] = now
        data["updated_at"] = now
        data["updated_by"] = actor
        revoked = ApiKey(**data)
        self._api_keys[api_key_id] = revoked
        return revoked

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        existing = self._api_keys.get(api_key_id)
        if existing is None:
            return
        data = existing.model_dump()
        data["last_used_at"] = _now()
        self._api_keys[api_key_id] = ApiKey(**data)

    # --- Providers ---

    def list_providers(self, *, active_only: bool = False) -> list[Provider]:
        providers = list(self._providers.values())
        if active_only:
            providers = [p for p in providers if p.active]
        return providers

    def get_provider(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    # --- Person identifiers ---

    def list_person_identifiers(self, person_id: UUID) -> list[PersonIdentifier]:
        return [i for i in self._identifiers.values() if i.person_id == person_id]

    def create_person_identifier(
        self, person_id: UUID, payload: PersonIdentifierCreate, *, actor: str
    ) -> PersonIdentifier:
        if payload.provider == "email":
            raise ValueError("email_not_addressable_by_provider")
        for i in self._identifiers.values():
            if i.person_id == person_id and i.provider == payload.provider:
                raise ValueError(
                    f"person already has an identifier for provider: {payload.provider}"
                )
            if i.provider == payload.provider and i.external_id == payload.external_id:
                raise ValueError(
                    f"identifier already linked to another person: "
                    f"{payload.provider}/{payload.external_id}"
                )
        now = _now()
        pi = PersonIdentifier(
            id=uuid4(),
            person_id=person_id,
            provider=payload.provider,
            external_id=payload.external_id,
            handle=payload.handle,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._identifiers[pi.id] = pi
        return pi

    def update_person_identifier(
        self, person_id: UUID, provider: str, payload: PersonIdentifierUpdate, *, actor: str
    ) -> PersonIdentifier | None:
        if provider == "email":
            raise ValueError("email_not_addressable_by_provider")
        existing = next(
            (
                i
                for i in self._identifiers.values()
                if i.person_id == person_id and i.provider == provider
            ),
            None,
        )
        if existing is None:
            return None
        patch = payload.model_dump(exclude_unset=True)
        new_external = patch.get("external_id")
        if new_external is not None and new_external != existing.external_id:
            if any(
                i.provider == provider and i.external_id == new_external and i.id != existing.id
                for i in self._identifiers.values()
            ):
                raise ValueError(
                    f"identifier already linked to another person: {provider}/{new_external}"
                )
        data = existing.model_dump()
        data.update(patch)
        data["updated_at"] = _now()
        data["updated_by"] = actor
        updated = PersonIdentifier(**data)
        self._identifiers[existing.id] = updated
        return updated

    def delete_person_identifier(self, person_id: UUID, provider: str) -> bool:
        if provider == "email":
            raise ValueError("email_not_addressable_by_provider")
        target = next(
            (
                i
                for i in self._identifiers.values()
                if i.person_id == person_id and i.provider == provider
            ),
            None,
        )
        if target is None:
            return False
        del self._identifiers[target.id]
        return True

    def get_person_by_identifier(self, provider: str, external_id: str) -> Person | None:
        target = _norm_email(external_id) if provider == "email" else external_id
        for i in self._identifiers.values():
            if i.provider == provider and i.external_id == target:
                return self._people.get(i.person_id)
        return None

    def add_person_email(self, person_id: UUID, email: str, *, actor: str) -> PersonIdentifier:
        addr = _norm_email(email)
        if not addr:
            raise ValueError("email_must_not_be_empty")
        # already an email identifier somewhere?
        for i in self._identifiers.values():
            if i.provider == "email" and i.external_id == addr:
                if i.person_id == person_id:
                    return i  # idempotent
                raise ValueError("email_registered_to_another")
        # another person's primary_email?
        owner = self.get_person_by_email(addr)
        if owner is not None and owner.id != person_id:
            raise ValueError("email_registered_to_another")
        now = _now()
        pi = PersonIdentifier(
            id=uuid4(),
            person_id=person_id,
            provider="email",
            external_id=addr,
            handle=None,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._identifiers[pi.id] = pi
        return pi
