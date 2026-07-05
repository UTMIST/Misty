from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from contracts.types import ApiKey
from src.storage.schema import api_keys


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _api_key_row_to_model(row) -> ApiKey:
    return ApiKey(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        active=row.active,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


class PostgresStorageAdapter:
    """Postgres-backed StorageAdapter using SQLAlchemy Core.

    Every method returns Pydantic domain models (never raw rows, and never the
    stored `key_hash`). Callers should not need to know this backend exists —
    they see StorageAdapter (Protocol).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
