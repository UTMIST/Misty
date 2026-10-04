from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import and_, delete, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

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
from src.storage.schema import (
    api_keys,
    people,
    person_identifiers,
    providers,
    role_kinds,
    team_memberships,
    teams,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _constraint_name(e: IntegrityError) -> str | None:
    return getattr(getattr(getattr(e, "orig", None), "diag", None), "constraint_name", None)


def _translate_membership_integrity_error(e: IntegrityError, payload=None) -> ValueError:
    """Map a membership IntegrityError to a ValueError so the router returns 400.

    Covers FK violations (bad person_id / team_id / role_kind_id) and the
    temporal-overlap EXCLUDE constraint (team_memberships_no_overlap).
    """
    constraint = _constraint_name(e)
    if constraint == "team_memberships_no_overlap":
        return ValueError(
            "membership overlaps an existing active membership for this person and team"
        )
    if payload is not None:
        return ValueError(
            "invalid membership reference: check person_id, team_id, and role_kind_id "
            f"(person_id={payload.person_id}, team_id={payload.team_id}, "
            f"role_kind_id={payload.role_kind_id})"
        )
    return ValueError("invalid membership reference: check role_kind_id")


def _norm_email(v: str) -> str:
    return v.strip().lower()


def _person_row_to_model(row) -> Person:
    return Person(
        id=row.id,
        display_name=row.display_name,
        primary_email=row.primary_email,
        active=row.active,
        access_level=row.access_level,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _team_row_to_model(row) -> Team:
    return Team(
        id=row.id,
        slug=row.slug,
        label=row.label,
        description=row.description,
        parent_id=row.parent_id,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _role_kind_row_to_model(row) -> RoleKind:
    return RoleKind(
        id=row.id,
        label=row.label,
        description=row.description,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _membership_row_to_model(row) -> TeamMembership:
    return TeamMembership(
        id=row.id,
        person_id=row.person_id,
        team_id=row.team_id,
        role_kind_id=row.role_kind_id,
        is_team_admin=row.is_team_admin,
        started_at=row.started_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _api_key_row_to_model(row) -> ApiKey:
    return ApiKey(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        active=row.active,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _provider_row_to_model(row) -> Provider:
    return Provider(
        id=row.id,
        label=row.label,
        description=row.description,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


def _identifier_row_to_model(row) -> PersonIdentifier:
    return PersonIdentifier(
        id=row.id,
        person_id=row.person_id,
        provider=row.provider,
        external_id=row.external_id,
        handle=row.handle,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
    )


class PostgresStorageAdapter:
    """Postgres-backed StorageAdapter using SQLAlchemy Core.

    Every method returns Pydantic domain models (never raw rows). Callers should
    not need to know this backend exists — they see StorageAdapter (Protocol).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def is_ready(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return False
        return True

    # --- People ---

    def create_person(self, payload: PersonCreate, *, actor: str) -> Person:
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    insert(people)
                    .values(
                        display_name=payload.display_name,
                        primary_email=payload.primary_email,
                        access_level=payload.access_level,
                        created_by=actor,
                        updated_by=actor,
                    )
                    .returning(people)
                ).one()
        except IntegrityError as e:
            raise ValueError(f"primary_email already exists: {payload.primary_email}") from e
        return _person_row_to_model(row)

    def get_person(self, person_id: UUID) -> Person | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(people).where(people.c.id == person_id)).one_or_none()
        return _person_row_to_model(row) if row else None

    def get_person_by_email(self, primary_email: str) -> Person | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(people).where(people.c.primary_email == primary_email)
            ).one_or_none()
        return _person_row_to_model(row) if row else None

    def list_people(self, *, active_only: bool = False) -> list[Person]:
        stmt = select(people)
        if active_only:
            stmt = stmt.where(people.c.active.is_(True))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_person_row_to_model(r) for r in rows]

    def update_person(self, person_id: UUID, payload: PersonUpdate, *, actor: str) -> Person | None:
        patch = payload.model_dump(exclude_unset=True)
        if not patch:
            return self.get_person(person_id)
        patch["updated_at"] = _now()
        patch["updated_by"] = actor
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    update(people).where(people.c.id == person_id).values(**patch).returning(people)
                ).one_or_none()
        except IntegrityError as e:
            raise ValueError(
                f"primary_email conflict on update: {patch.get('primary_email')}"
            ) from e
        return _person_row_to_model(row) if row else None

    # --- Teams ---

    def create_team(self, payload: TeamCreate, *, actor: str) -> Team:
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    insert(teams)
                    .values(
                        slug=payload.slug,
                        label=payload.label,
                        description=payload.description,
                        parent_id=payload.parent_id,
                        created_by=actor,
                        updated_by=actor,
                    )
                    .returning(teams)
                ).one()
        except IntegrityError as e:
            constraint = getattr(
                getattr(getattr(e, "orig", None), "diag", None), "constraint_name", None
            )
            if constraint == "teams_parent_id_fkey":
                raise UnknownParentTeamError(payload.parent_id) from e
            raise ValueError(f"slug already exists: {payload.slug}") from e
        return _team_row_to_model(row)

    def get_team(self, team_id: UUID) -> Team | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(teams).where(teams.c.id == team_id)).one_or_none()
        return _team_row_to_model(row) if row else None

    def get_team_by_slug(self, slug: str) -> Team | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(teams).where(teams.c.slug == slug)).one_or_none()
        return _team_row_to_model(row) if row else None

    def list_teams(self, *, active_only: bool = False) -> list[Team]:
        stmt = select(teams)
        if active_only:
            stmt = stmt.where(teams.c.active.is_(True))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_team_row_to_model(r) for r in rows]

    def update_team(self, team_id: UUID, payload: TeamUpdate, *, actor: str) -> Team | None:
        patch = payload.model_dump(exclude_unset=True)
        if not patch:
            return self.get_team(team_id)
        patch["updated_at"] = _now()
        patch["updated_by"] = actor
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    update(teams).where(teams.c.id == team_id).values(**patch).returning(teams)
                ).one_or_none()
        except IntegrityError as e:
            raise ValueError(f"slug conflict on update: {patch.get('slug')}") from e
        return _team_row_to_model(row) if row else None

    # --- Role kinds ---

    def get_role_kind(self, role_kind_id: str) -> RoleKind | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(role_kinds).where(role_kinds.c.id == role_kind_id)
            ).one_or_none()
        return _role_kind_row_to_model(row) if row else None

    def list_role_kinds(self, *, active_only: bool = False) -> list[RoleKind]:
        stmt = select(role_kinds)
        if active_only:
            stmt = stmt.where(role_kinds.c.active.is_(True))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_role_kind_row_to_model(r) for r in rows]

    # --- Team memberships ---

    def create_membership(self, payload: TeamMembershipCreate, *, actor: str) -> TeamMembership:
        values = {
            "person_id": payload.person_id,
            "team_id": payload.team_id,
            "role_kind_id": payload.role_kind_id,
            "is_team_admin": payload.is_team_admin,
            "ended_at": payload.ended_at,
            "created_by": actor,
            "updated_by": actor,
        }
        if payload.started_at is not None:
            values["started_at"] = payload.started_at
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    insert(team_memberships).values(**values).returning(team_memberships)
                ).one()
        except IntegrityError as e:
            raise _translate_membership_integrity_error(e, payload) from e
        return _membership_row_to_model(row)

    def get_membership(self, membership_id: UUID) -> TeamMembership | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(team_memberships).where(team_memberships.c.id == membership_id)
            ).one_or_none()
        return _membership_row_to_model(row) if row else None

    def list_memberships(
        self,
        *,
        team_id: UUID | None = None,
        person_id: UUID | None = None,
        active_only: bool = False,
        as_of: date | None = None,
        is_team_admin: bool | None = None,
    ) -> list[TeamMembership]:
        stmt = select(team_memberships)
        conditions = []
        if team_id is not None:
            conditions.append(team_memberships.c.team_id == team_id)
        if person_id is not None:
            conditions.append(team_memberships.c.person_id == person_id)
        if active_only:
            # "Currently active" = open-ended OR ends strictly after today. A
            # future-dated ended_at means the membership is still active now, so
            # a bare `ended_at IS NULL` would wrongly drop future-dated rows.
            conditions.append(
                or_(
                    team_memberships.c.ended_at.is_(None),
                    team_memberships.c.ended_at > func.current_date(),
                )
            )
        if as_of is not None:
            conditions.append(team_memberships.c.started_at <= as_of)
            conditions.append(
                or_(
                    team_memberships.c.ended_at.is_(None),
                    team_memberships.c.ended_at > as_of,
                )
            )
        if is_team_admin is not None:
            conditions.append(team_memberships.c.is_team_admin.is_(is_team_admin))
        if conditions:
            stmt = stmt.where(and_(*conditions))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_membership_row_to_model(r) for r in rows]

    def update_membership(
        self, membership_id: UUID, payload: TeamMembershipUpdate, *, actor: str
    ) -> TeamMembership | None:
        patch = payload.model_dump(exclude_unset=True)
        if not patch:
            return self.get_membership(membership_id)
        patch["updated_at"] = _now()
        patch["updated_by"] = actor
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    update(team_memberships)
                    .where(team_memberships.c.id == membership_id)
                    .values(**patch)
                    .returning(team_memberships)
                ).one_or_none()
        except IntegrityError as e:
            # A bad role_kind_id FK, or re-opening a membership (moving ended_at
            # into the future) that now overlaps another active one.
            raise _translate_membership_integrity_error(e) from e
        return _membership_row_to_model(row) if row else None

    def end_membership(
        self, membership_id: UUID, ended_at: date, *, actor: str
    ) -> TeamMembership | None:
        return self.update_membership(
            membership_id, TeamMembershipUpdate(ended_at=ended_at), actor=actor
        )

    # --- Providers ---

    def list_providers(self, *, active_only: bool = False) -> list[Provider]:
        stmt = select(providers)
        if active_only:
            stmt = stmt.where(providers.c.active.is_(True))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_provider_row_to_model(r) for r in rows]

    def get_provider(self, provider_id: str) -> Provider | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(providers).where(providers.c.id == provider_id)).one_or_none()
        return _provider_row_to_model(row) if row else None

    # --- Person identifiers ---

    def list_person_identifiers(self, person_id: UUID) -> list[PersonIdentifier]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(person_identifiers).where(person_identifiers.c.person_id == person_id)
            ).all()
        return [_identifier_row_to_model(r) for r in rows]

    def create_person_identifier(
        self, person_id: UUID, payload: PersonIdentifierCreate, *, actor: str
    ) -> PersonIdentifier:
        if payload.provider == "email":
            raise ValueError("email_not_addressable_by_provider")
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    insert(person_identifiers)
                    .values(
                        person_id=person_id,
                        provider=payload.provider,
                        external_id=payload.external_id,
                        handle=payload.handle,
                        created_by=actor,
                        updated_by=actor,
                    )
                    .returning(person_identifiers)
                ).one()
        except IntegrityError as e:
            constraint = getattr(
                getattr(getattr(e, "orig", None), "diag", None), "constraint_name", None
            )
            if constraint == "uq_person_identifiers_person_provider":
                raise ValueError(
                    f"person already has an identifier for provider: {payload.provider}"
                ) from e
            if constraint == "uq_person_identifiers_provider_external":
                raise ValueError(
                    f"identifier already linked to another person: "
                    f"{payload.provider}/{payload.external_id}"
                ) from e
            raise ValueError(f"identifier conflict for provider {payload.provider}") from e
        return _identifier_row_to_model(row)

    def update_person_identifier(
        self, person_id: UUID, provider: str, payload: PersonIdentifierUpdate, *, actor: str
    ) -> PersonIdentifier | None:
        if provider == "email":
            raise ValueError("email_not_addressable_by_provider")
        patch = payload.model_dump(exclude_unset=True)
        patch["updated_at"] = _now()
        patch["updated_by"] = actor
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    update(person_identifiers)
                    .where(
                        person_identifiers.c.person_id == person_id,
                        person_identifiers.c.provider == provider,
                    )
                    .values(**patch)
                    .returning(person_identifiers)
                ).one_or_none()
        except IntegrityError as e:
            raise ValueError(f"external_id conflict on update for provider {provider}") from e
        return _identifier_row_to_model(row) if row else None

    def delete_person_identifier(self, person_id: UUID, provider: str) -> bool:
        if provider == "email":
            raise ValueError("email_not_addressable_by_provider")
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(person_identifiers).where(
                    person_identifiers.c.person_id == person_id,
                    person_identifiers.c.provider == provider,
                )
            )
        return result.rowcount > 0

    def get_person_by_identifier(self, provider: str, external_id: str) -> Person | None:
        target = _norm_email(external_id) if provider == "email" else external_id
        with self._engine.connect() as conn:
            row = conn.execute(
                select(people)
                .select_from(
                    person_identifiers.join(people, person_identifiers.c.person_id == people.c.id)
                )
                .where(
                    person_identifiers.c.provider == provider,
                    person_identifiers.c.external_id == target,
                )
            ).one_or_none()
        return _person_row_to_model(row) if row else None

    def add_person_email(self, person_id: UUID, email: str, *, actor: str) -> PersonIdentifier:
        addr = _norm_email(email)
        if not addr:
            raise ValueError("email_must_not_be_empty")
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(person_identifiers).where(
                    person_identifiers.c.provider == "email",
                    person_identifiers.c.external_id == addr,
                )
            ).one_or_none()
            if existing is not None:
                if existing.person_id == person_id:
                    return _identifier_row_to_model(existing)
                raise ValueError("email_registered_to_another")
            primary_owner = conn.execute(
                select(people.c.id).where(people.c.primary_email == addr)
            ).scalar_one_or_none()
            if primary_owner is not None and primary_owner != person_id:
                raise ValueError("email_registered_to_another")
            row = conn.execute(
                pg_insert(person_identifiers)
                .values(
                    person_id=person_id,
                    provider="email",
                    external_id=addr,
                    handle=None,
                    created_by=actor,
                    updated_by=actor,
                )
                .on_conflict_do_nothing(constraint="uq_person_identifiers_provider_external")
                .returning(person_identifiers)
            ).one_or_none()
            if row is not None:
                return _identifier_row_to_model(row)
            # Lost a concurrent insert race on our own (person_id, addr) pair, or
            # someone else grabbed this address between our pre-check and insert.
            # Re-read to disambiguate: same person -> idempotent; else -> conflict.
            conflicting = conn.execute(
                select(person_identifiers).where(
                    person_identifiers.c.provider == "email",
                    person_identifiers.c.external_id == addr,
                )
            ).one_or_none()
            if conflicting is not None and conflicting.person_id == person_id:
                return _identifier_row_to_model(conflicting)
            raise ValueError("email_registered_to_another")

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
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    insert(api_keys)
                    .values(
                        name=name,
                        prefix=prefix,
                        key_hash=key_hash,
                        scopes=scopes,
                        created_by=actor,
                        updated_by=actor,
                    )
                    .returning(api_keys)
                ).one()
        except IntegrityError as e:
            raise ValueError(f"name or prefix already exists: {name!r} / {prefix!r}") from e
        return _api_key_row_to_model(row)

    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(api_keys).where(api_keys.c.prefix == prefix)).one_or_none()
        return _api_key_row_to_model(row) if row else None

    def get_api_key_hash(self, prefix: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(api_keys.c.key_hash).where(
                    api_keys.c.prefix == prefix,
                    api_keys.c.active.is_(True),
                    api_keys.c.revoked_at.is_(None),
                )
            ).one_or_none()
        return row.key_hash if row else None

    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]:
        stmt = select(api_keys)
        if active_only:
            stmt = stmt.where(
                api_keys.c.active.is_(True),
                api_keys.c.revoked_at.is_(None),
            )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [_api_key_row_to_model(r) for r in rows]

    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None:
        now = _now()
        with self._engine.begin() as conn:
            row = conn.execute(
                update(api_keys)
                .where(api_keys.c.id == api_key_id)
                .values(active=False, revoked_at=now, updated_at=now, updated_by=actor)
                .returning(api_keys)
            ).one_or_none()
        return _api_key_row_to_model(row) if row else None

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(api_keys).where(api_keys.c.id == api_key_id).values(last_used_at=_now())
                )
        except Exception:
            pass  # best-effort; DB blips must not fail the auth path
