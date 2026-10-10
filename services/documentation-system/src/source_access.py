"""Resolve source ACL evidence and reconcile derived document grants."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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


def resolve_google_permissions(
    permissions: list[SourcePermission], directory: DirectoryClient
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
                warnings.append(f"expired source permission ignored: {permission.permission_id}")
                continue
            if permission.principal_type == "user":
                if not principal:
                    raise SourceAccessUnavailable("source user permission omitted its email")
                grantee_id = directory.get_person_id_by_verified_email(principal)
                if grantee_id is None:
                    warnings.append(f"unmapped source user ignored: {principal}")
                    continue
                grantee_type = "person"
            elif permission.principal_type == "group":
                if not principal:
                    raise SourceAccessUnavailable("source group permission omitted its email")
                grantee_id = directory.get_team_id_by_synced_google_group(principal)
                if grantee_id is None:
                    warnings.append(f"unmapped or unsynchronized source group ignored: {principal}")
                    continue
                grantee_type = "team"
            elif permission.principal_type in {"domain", "anyone"}:
                label = principal or permission.principal_type
                warnings.append(f"broad source share ignored by policy: {label}")
                continue
            else:  # defensive for non-Pydantic test doubles
                warnings.append(
                    f"unsupported source principal ignored: {permission.principal_type}"
                )
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
