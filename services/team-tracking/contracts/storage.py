from datetime import date
from typing import Protocol
from uuid import UUID

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


class StorageAdapter(Protocol):
    """Stable internal contract between the API and the persistence layer.

    Any concrete adapter (Postgres, SQLite, in-memory, ...) implements these
    methods with identical semantics. Consumers of the API never see this
    interface — only the API layer does.

    Conventions:
    - All methods that write also stamp created_by/updated_by using the actor arg.
    - `list_*` methods that take filters return every matching record; pagination
      is not implemented in v1 (add later if the roster grows past ~500 people).
    - `end_membership` is a semantic helper: sets ended_at without deleting the row.
    - Records are never hard-deleted; use `active=False` on Person/Team, or
      `end_membership` for soft-removal.
    """

    def is_ready(self) -> bool:
        """Return whether the backing store can answer a trivial query."""
        ...

    # People
    def create_person(self, payload: PersonCreate, *, actor: str) -> Person: ...
    def get_person(self, person_id: UUID) -> Person | None: ...
    def get_person_by_email(self, primary_email: str) -> Person | None: ...
    def list_people(self, *, active_only: bool = False) -> list[Person]:
        """List every person. To fetch people for a specific team, use
        `list_memberships(team_id=...)` and resolve `person_id` values."""
        ...

    def update_person(
        self, person_id: UUID, payload: PersonUpdate, *, actor: str
    ) -> Person | None: ...

    # Teams
    def create_team(self, payload: TeamCreate, *, actor: str) -> Team: ...
    def get_team(self, team_id: UUID) -> Team | None: ...
    def get_team_by_slug(self, slug: str) -> Team | None: ...
    def list_teams(self, *, active_only: bool = False) -> list[Team]: ...
    def update_team(self, team_id: UUID, payload: TeamUpdate, *, actor: str) -> Team | None: ...

    # Role kinds
    def get_role_kind(self, role_kind_id: str) -> RoleKind | None: ...
    def list_role_kinds(self, *, active_only: bool = False) -> list[RoleKind]: ...

    # Team memberships
    def create_membership(self, payload: TeamMembershipCreate, *, actor: str) -> TeamMembership: ...
    def get_membership(self, membership_id: UUID) -> TeamMembership | None: ...
    def list_memberships(
        self,
        *,
        team_id: UUID | None = None,
        person_id: UUID | None = None,
        active_only: bool = False,
        as_of: date | None = None,
        is_team_admin: bool | None = None,
    ) -> list[TeamMembership]:
        """List memberships matching every provided filter.

        `as_of`: return only memberships active on that date — i.e. rows where
        `started_at <= as_of` AND (`ended_at IS NULL` OR `ended_at > as_of`).
        """
        ...

    def update_membership(
        self, membership_id: UUID, payload: TeamMembershipUpdate, *, actor: str
    ) -> TeamMembership | None: ...
    def end_membership(
        self, membership_id: UUID, ended_at: date, *, actor: str
    ) -> TeamMembership | None:
        """Set `ended_at` without touching role_kind or admin flag.

        Prefer this over `update_membership` for soft-closes so intent is clear
        at the call site. `update_membership` accepts `ended_at` too, but is
        semantically the general edit path.
        """
        ...

    # API keys (Level 2 security)
    def create_api_key(
        self,
        *,
        name: str,
        prefix: str,
        key_hash: str,
        scopes: list[str],
        actor: str,
    ) -> ApiKey: ...
    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        """Return the ApiKey row for a given prefix. Includes the key_hash for
        the caller to verify against a candidate plaintext.

        The prefix is a public identifier (first 8 chars of the key) — lookup
        by prefix is safe."""
        ...

    def get_api_key_hash(self, prefix: str) -> str | None:
        """Return the argon2 key_hash for a prefix, or None if not found or revoked.
        Separate from get_api_key_by_prefix so the public ApiKey model never has
        to carry the hash string in its Pydantic shape."""
        ...

    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]: ...
    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None:
        """Mark a key revoked (sets revoked_at, active=false). Never hard-deletes."""
        ...

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        """Idempotently update last_used_at. Callers should invoke on every
        successful auth. Failure to update should not fail the request (best-effort)."""
        ...

    # Providers (identity vocabulary — read-only through the API)
    def list_providers(self, *, active_only: bool = False) -> list[Provider]: ...
    def get_provider(self, provider_id: str) -> Provider | None: ...

    # Person identifiers (external account links)
    def list_person_identifiers(self, person_id: UUID) -> list[PersonIdentifier]: ...
    def create_person_identifier(
        self, person_id: UUID, payload: PersonIdentifierCreate, *, actor: str
    ) -> PersonIdentifier:
        """Link an external account to a person. Raises ValueError if the person
        already has this provider linked, or the (provider, external_id) already
        belongs to another person. Does not validate person/provider existence —
        the router pre-checks those to map 404 vs 400.

        Rejects provider='email' with ValueError('email_not_addressable_by_provider')
        — email is multi-valued; use `add_person_email` instead."""
        ...

    def add_person_email(self, person_id: UUID, email: str, *, actor: str) -> PersonIdentifier:
        """Attach a verified email (provider='email') to a person.

        Normalizes the address; raises ValueError('email_registered_to_another')
        if another person owns it (as primary_email or an email identifier);
        idempotently returns the caller's existing email identifier if present.
        The generic create/update/delete_person_identifier reject provider='email'
        (multi-valued; use this method / the /emails endpoint instead).
        """
        ...

    def update_person_identifier(
        self, person_id: UUID, provider: str, payload: PersonIdentifierUpdate, *, actor: str
    ) -> PersonIdentifier | None:
        """Update external_id/handle of the (person_id, provider) link. Returns
        None if no such link. Raises ValueError if a new external_id collides
        with another person's link on the same provider.

        Rejects provider='email' with ValueError('email_not_addressable_by_provider')
        — email is multi-valued; use `add_person_email` instead."""
        ...

    def delete_person_identifier(self, person_id: UUID, provider: str) -> bool:
        """Hard-delete the (person_id, provider) link. True if removed, False if
        no such link. Identity mappings are current state, not history.

        Rejects provider='email' with ValueError('email_not_addressable_by_provider')
        — email is multi-valued; deletion is not exposed for it here."""
        ...

    def get_person_by_identifier(self, provider: str, external_id: str) -> Person | None:
        """Reverse lookup: resolve an external account to its Person, or None.
        For provider='email' the external_id is normalized before matching."""
        ...
