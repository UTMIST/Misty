from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import UUID, uuid4

from contracts.types import ApiKey
from contracts.login import LoginChallenge
from src import login_policy as policy


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryStorageAdapter:
    """In-process adapter for tests. Not persistent, not thread-safe."""

    def __init__(self) -> None:
        self._login_lock = Lock()
        self._logins: dict[UUID, LoginChallenge] = {}
        self._limits: dict[str, tuple[int, datetime]] = {}
        self._api_keys: dict[UUID, ApiKey] = {}
        self._hashes: dict[UUID, str] = {}

    def _spend(self, limits: list[policy.Limit], now: datetime) -> bool:
        for key, maximum, _ in limits:
            count, expires = self._limits.get(key, (0, now))
            if expires > now and count >= maximum:
                return False
        for key, _, seconds in limits:
            count, expires = self._limits.get(key, (0, now))
            if expires <= now:
                count, expires = 0, now + timedelta(seconds=seconds)
            self._limits[key] = (count + 1, expires)
        return True

    def allow_login_request(self, owner, operation, client_ip, now):
        with self._login_lock:
            self._logins = {k: v for k, v in self._logins.items() if v.expires_at > now}
            self._limits = {k: v for k, v in self._limits.items() if v[1] > now}
            return self._spend(policy.request_limits(owner, operation, client_ip), now)

    def reserve_login(self, challenge: LoginChallenge) -> bool:
        with self._login_lock:
            if not self._spend(policy.send_limits(challenge.email), challenge.created_at):
                return False
            self._logins[challenge.id] = challenge.model_copy()
            return True

    def activate_login(self, challenge_id, person_id, now):
        with self._login_lock:
            item = self._logins.get(challenge_id)
            if item is None or item.expires_at <= now or item.redeemed:
                return False
            if any(
                v.owner == item.owner
                and v.email == item.email
                and v.person_id is not None
                and v.created_at > item.created_at
                for v in self._logins.values()
            ):
                return False
            self._logins = {
                k: v
                for k, v in self._logins.items()
                if not (
                    v.owner == item.owner
                    and v.email == item.email
                    and v.created_at < item.created_at
                )
            }
            self._logins[challenge_id] = item.model_copy(update={"person_id": person_id})
            return True

    def admit_login_attempt(self, challenge_id, owner, now):
        with self._login_lock:
            item = self._logins.get(challenge_id)
            if (
                item is None
                or item.owner != owner
                or item.expires_at <= now
                or item.redeemed
                or item.attempts >= policy.MAX_ATTEMPTS
            ):
                return None
            if not self._spend(policy.guess_limits(item.email), now):
                return None
            item = item.model_copy(update={"attempts": item.attempts + 1})
            self._logins[challenge_id] = item
            return item.model_copy()

    def redeem_login(self, challenge_id, owner, now):
        with self._login_lock:
            item = self._logins.get(challenge_id)
            if (
                item is None
                or item.owner != owner
                or item.expires_at <= now
                or item.redeemed
                or item.person_id is None
            ):
                return False
            self._logins[challenge_id] = item.model_copy(update={"redeemed": True})
            return True

    def is_ready(self) -> bool:
        return True

    def create_api_key(
        self, *, name: str, prefix: str, key_hash: str, scopes: list[str], actor: str
    ) -> ApiKey:
        if any(k.name == name for k in self._api_keys.values()):
            raise ValueError(f"api key name already exists: {name}")
        if any(k.prefix == prefix for k in self._api_keys.values()):
            raise ValueError(f"api key prefix already exists: {prefix}")
        now = _now()
        key = ApiKey(
            id=uuid4(),
            name=name,
            prefix=prefix,
            scopes=list(scopes),
            active=True,
            created_at=now,
            updated_at=now,
            created_by=actor,
            updated_by=actor,
        )
        self._api_keys[key.id] = key
        self._hashes[key.id] = key_hash
        return key

    def _by_prefix(self, prefix: str) -> ApiKey | None:
        return next((k for k in self._api_keys.values() if k.prefix == prefix), None)

    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None:
        return self._by_prefix(prefix)

    def get_api_key_hash(self, prefix: str) -> str | None:
        row = self._by_prefix(prefix)
        return self._hashes.get(row.id) if row and row.active and row.revoked_at is None else None

    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]:
        keys = list(self._api_keys.values())
        if active_only:
            keys = [k for k in keys if k.active and k.revoked_at is None]
        return keys

    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None:
        key = self._api_keys.get(api_key_id)
        if key is None:
            return None
        if key.revoked_at is not None:
            return key
        now = _now()
        updated = key.model_copy(
            update={"active": False, "revoked_at": now, "updated_at": now, "updated_by": actor}
        )
        self._api_keys[api_key_id] = updated
        return updated

    def touch_api_key_last_used(self, api_key_id: UUID) -> None:
        key = self._api_keys.get(api_key_id)
        if key is not None:
            self._api_keys[api_key_id] = key.model_copy(update={"last_used_at": _now()})
