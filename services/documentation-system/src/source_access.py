"""Resolve source ACL evidence and reconcile derived document grants."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from contracts.directory import DirectoryClient, DirectoryUnavailable
from contracts.fetcher import SourcePermission
from contracts.types import SourceGrant

GOOGLE_ACCESS_ORIGIN = "google_drive"
GOOGLE_SOURCE_IDS = frozenset({"gdocs", "gsheets", "gslides", "gdrive"})


class SourceAccessUnavailable(Exception):
    """The source ACL could not be resolved completely; retain prior grants."""


@dataclass(frozen=True)
class ResolvedSourceAccess:
    grants: list[SourceGrant]
    warnings: list[str]


@dataclass
class SourceIdentityCache:
    """Identity results for one refresh batch; never cache ACLs or outages."""

    people: dict[str, UUID | None] = field(default_factory=dict)
    groups: dict[str, UUID | None] = field(default_factory=dict)


def resolve_google_permissions(
    permissions: list[SourcePermission],
    directory: DirectoryClient,
    *,
    identity_cache: SourceIdentityCache | None = None,
) -> ResolvedSourceAccess:
    """Resolve Google ACL principals without inferring identities or membership.

    Unknown users/groups and broad shares produce no grant. Any directory
    outage aborts the whole reconciliation so callers never replace a known
    ACL with a partial result.
    """
    grants: list[SourceGrant] = []
    warnings: list[str] = []
    seen: set[tuple[str, object, str]] = set()

    try:
        for permission in permissions:
            principal = (permission.principal or "").strip().lower()
            expiration_time = permission.expiration_time
            if expiration_time is not None and expiration_time.tzinfo is None:
                expiration_time = expiration_time.replace(tzinfo=timezone.utc)
            if expiration_time is not None and expiration_time <= datetime.now(timezone.utc):
                warnings.append("expired source permission ignored")
                continue
            if permission.principal_type == "user":
                if not principal:
                    raise SourceAccessUnavailable("source user permission omitted its email")
                if identity_cache is not None and principal in identity_cache.people:
                    grantee_id = identity_cache.people[principal]
                else:
                    grantee_id = directory.get_person_id_by_verified_email(principal)
                    if identity_cache is not None:
                        identity_cache.people[principal] = grantee_id
                if grantee_id is None:
                    warnings.append("unmapped source user ignored")
                    continue
                grantee_type = "person"
            elif permission.principal_type == "group":
                if not principal:
                    raise SourceAccessUnavailable("source group permission omitted its email")
                if identity_cache is not None and principal in identity_cache.groups:
                    grantee_id = identity_cache.groups[principal]
                else:
                    grantee_id = directory.get_team_id_by_synced_google_group(principal)
                    if identity_cache is not None:
                        identity_cache.groups[principal] = grantee_id
                if grantee_id is None:
                    warnings.append("unmapped or unsynchronized source group ignored")
                    continue
                grantee_type = "team"
            elif permission.principal_type in {"domain", "anyone"}:
                warnings.append("broad source share ignored by policy")
                continue
            else:  # defensive for non-Pydantic test doubles
                warnings.append("unsupported source principal ignored")
                continue

            key = (grantee_type, grantee_id, permission.permission_id)
            if key in seen:
                continue
            seen.add(key)
            grants.append(
                SourceGrant(
                    grantee_type=grantee_type,
                    grantee_id=grantee_id,
                    source_permission_id=permission.permission_id,
                    source_principal=principal,
                    source_role=permission.role,
                    source_inherited=permission.inherited,
                    source_inherited_from=list(permission.inherited_from),
                    source_expires_at=expiration_time,
                )
            )
    except DirectoryUnavailable as e:
        raise SourceAccessUnavailable("directory unavailable during source ACL resolution") from e

    return ResolvedSourceAccess(grants=grants, warnings=warnings)


def source_grant_expiry(synced_at: datetime, max_age_hours: int) -> datetime:
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    return synced_at + timedelta(hours=max_age_hours)
