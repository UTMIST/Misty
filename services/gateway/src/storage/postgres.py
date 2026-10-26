import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from contracts.types import ApiKey
from contracts.login import LoginChallenge
from src import login_policy as policy
from src.storage.schema import api_keys, login_challenges, login_limits

logger = logging.getLogger("gateway.storage")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _api_key_row_to_model(row) -> ApiKey:
    return ApiKey(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
        created_by=row.created_by,
        updated_by=row.updated_by,
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

    @contextmanager
    def _login_transaction(self):
        # One short cross-process lock for this low-volume public auth boundary.
        # Never hold it across HTTP. All login writes go through this lock.
        with self._engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(71329002)"))
            yield conn

    def _spend(self, conn, limits: list[policy.Limit], now: datetime) -> bool:
        rows = {
            r.bucket: r
            for r in conn.execute(
                select(login_limits).where(login_limits.c.bucket.in_([x[0] for x in limits]))
            )
        }
        for bucket, maximum, _ in limits:
            row = rows.get(bucket)
            if row is not None and row.expires_at > now and row.count >= maximum:
                return False
        for bucket, _, seconds in limits:
            row = rows.get(bucket)
            live = row is not None and row.expires_at > now
            values = dict(
                bucket=bucket,
                count=row.count + 1 if live else 1,
                expires_at=row.expires_at if live else now + timedelta(seconds=seconds),
            )
            conn.execute(
                pg_insert(login_limits)
                .values(**values)
                .on_conflict_do_update(index_elements=[login_limits.c.bucket], set_=values)
            )
        return True

    def allow_login_request(self, owner, operation, client_ip, now, *, status_limit=600):
        with self._login_transaction() as conn:
            conn.execute(delete(login_challenges).where(login_challenges.c.expires_at <= now))
            conn.execute(delete(login_limits).where(login_limits.c.expires_at <= now))
            return self._spend(
                conn,
                policy.request_limits(owner, operation, client_ip, status_limit=status_limit),
                now,
            )

    def reserve_login(self, challenge: LoginChallenge) -> bool:
        with self._login_transaction() as conn:
            if not self._spend(conn, policy.send_limits(challenge.email), challenge.created_at):
                return False
            conn.execute(insert(login_challenges).values(**challenge.model_dump()))
            return True

    def activate_login(self, challenge_id, person_id, now):
        with self._login_transaction() as conn:
            item = conn.execute(
                select(login_challenges).where(
                    login_challenges.c.id == challenge_id,
                    login_challenges.c.expires_at > now,
                    login_challenges.c.redeemed.is_(False),
                )
            ).one_or_none()
            if item is None:
                return False
            return (
                conn.execute(
                    update(login_challenges)
                    .where(
                        login_challenges.c.id == challenge_id,
                        login_challenges.c.expires_at > now,
                        login_challenges.c.redeemed.is_(False),
                    )
                    .values(person_id=person_id)
                ).rowcount
                == 1
            )

    def admit_login_attempt(self, challenge_id, owner, now):
        with self._login_transaction() as conn:
            row = conn.execute(
                select(login_challenges).where(
                    login_challenges.c.id == challenge_id,
                    login_challenges.c.owner == owner,
                    login_challenges.c.expires_at > now,
                    login_challenges.c.redeemed.is_(False),
                    login_challenges.c.attempts < policy.MAX_ATTEMPTS,
                )
            ).one_or_none()
            if row is None:
                return None
            updated = conn.execute(
                update(login_challenges)
                .where(login_challenges.c.id == challenge_id)
                .values(attempts=login_challenges.c.attempts + 1)
                .returning(login_challenges)
            ).one()
            return LoginChallenge.model_validate(updated._mapping)

    def redeem_login(self, challenge_id, owner, now):
        with self._login_transaction() as conn:
            return (
                conn.execute(
                    update(login_challenges)
                    .where(
                        login_challenges.c.id == challenge_id,
                        login_challenges.c.owner == owner,
                        login_challenges.c.expires_at > now,
                        login_challenges.c.redeemed.is_(False),
                        login_challenges.c.person_id.is_not(None),
                    )
                    .values(redeemed=True)
                ).rowcount
                == 1
            )

    def is_ready(self) -> bool:
        try:
            with self._engine.connect() as conn:
                # Authentication needs the migrated key table, not just a live server.
                conn.execute(select(api_keys.c.id).limit(1))
                conn.execute(select(login_challenges.c.id).limit(1))
                conn.execute(select(login_limits.c.bucket).limit(1))
        except SQLAlchemyError:
            return False
        return True

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
                .where(api_keys.c.id == api_key_id, api_keys.c.revoked_at.is_(None))
                .values(active=False, revoked_at=now, updated_at=now, updated_by=actor)
                .returning(api_keys)
            ).one_or_none()
            if row is None:
                row = conn.execute(
                    select(api_keys).where(api_keys.c.id == api_key_id)
                ).one_or_none()
        return _api_key_row_to_model(row) if row else None

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(api_keys).where(api_keys.c.id == api_key_id).values(last_used_at=_now())
                )
        except Exception:
            # Best-effort: a DB blip must not fail the auth path over a
            # bookkeeping write. Logged rather than swallowed outright, because
            # silence here means last_used_at can go stale indefinitely and the
            # only symptom is a key that looks unused while it is in daily use.
            logger.warning(
                "could not update last_used_at for api key %s", api_key_id, exc_info=True
            )
