from datetime import datetime
from typing import Protocol
from uuid import UUID

from contracts.types import ApiKey
from contracts.login import LoginChallenge


class StorageAdapter(Protocol):
    def allow_login_request(
        self,
        owner: str,
        operation: str,
        client_ip: str | None,
        now: datetime,
        *,
        status_limit: int = 600,
    ) -> bool:
        """Atomically spend app and attested-IP quotas; prune expired login state."""
        ...

    def reserve_login(self, challenge: LoginChallenge) -> bool:
        """Atomically reserve mailbox send quotas and persist a pending challenge.

        Quotas apply equally to unknown/inactive mailboxes and are never refunded.
        Other challenges remain independently usable until expiry or redemption.
        """
        ...

    def activate_login(self, challenge_id: UUID, person_id: UUID, now: datetime) -> bool:
        """Bind after delivery without changing any other challenge's validity."""
        ...

    def admit_login_attempt(
        self, challenge_id: UUID, owner: str, now: datetime
    ) -> LoginChallenge | None:
        """Atomically spend this challenge's attempt budget before verification."""
        ...

    def redeem_login(self, challenge_id: UUID, owner: str, now: datetime) -> bool:
        """Single-use compare-and-set; exactly one concurrent redemption can succeed."""
        ...

    def is_ready(self) -> bool: ...
    def create_api_key(
        self, *, name: str, prefix: str, key_hash: str, scopes: list[str], actor: str
    ) -> ApiKey: ...
    def get_api_key_by_prefix(self, prefix: str) -> ApiKey | None: ...
    def get_api_key_hash(self, prefix: str) -> str | None: ...
    def list_api_keys(self, *, active_only: bool = False) -> list[ApiKey]: ...
    def revoke_api_key(self, api_key_id: UUID, *, actor: str) -> ApiKey | None: ...
    def touch_api_key_last_used(self, api_key_id: UUID) -> None: ...
