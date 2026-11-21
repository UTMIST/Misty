"""Reconcile each team's managed Google Group with Team Tracking membership.

Team Tracking is the source of truth. A sync computes the emails that should
be in the group, reads who actually is, and adds/removes the difference, so
every run is idempotent and retrying is just running it again.
"""

import threading
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import BackgroundTasks

from contracts.groups import (
    ExternalMembersDisabled,
    GroupOperationPending,
    GroupProvider,
    GroupProviderError,
    MembershipIdentityRejected,
)
from contracts.storage import StorageAdapter
from contracts.types import GoogleGroupStatus, PendingGroupChange, Person, Team, TeamGoogleGroup

# Queues this process's syncs so each waits for the cross-process team lock on
# one pool connection, instead of every queued sync holding one.
_lock = threading.Lock()

EXTERNAL_MEMBERS_HINT = (
    "group still rejects external members after automatic settings verification: "
    "ask UTMIST's Google administrator to check 'Allow external members' and "
    "external-member policy, then re-sync"
)


def desired_members(storage: StorageAdapter, team_id: UUID) -> dict[str, set[str]]:
    """Map each desired group address to the primary addresses of its active owners."""
    team = storage.get_team(team_id)
    if team is None or not team.active:
        return {}
    owners: dict[str, set[str]] = {}
    for m in storage.list_memberships(team_id=team_id, as_of=date.today()):
        person = storage.get_person(m.person_id)
        if person is not None and person.active:
            for email in person_emails(storage, person):
                owners.setdefault(email, set()).add(person.primary_email)
    return owners


def person_emails(storage: StorageAdapter, person: Person) -> set[str]:
    """`primary_email` plus every verified `email` identifier."""
    return {person.primary_email} | {
        i.external_id for i in storage.list_person_identifiers(person.id) if i.provider == "email"
    }


def synced_team_for_group(
    storage: StorageAdapter, group_email: str, *, include_individual: bool = False
) -> Team | None:
    """Map a Drive ACL principal. Shared audiences always require a fully synced group."""
    addr = group_email.strip().lower()
    for g in storage.list_team_google_groups():
        if g.group_email.lower() != addr:
            continue
        if g.status == "synced":
            return storage.get_team(g.team_id)
        if include_individual and g.permits_individual_lookup:
            team = storage.get_team(g.team_id)
            if team and team.active:
                return team
    return None


def _confirmed_individual_member(
    storage: StorageAdapter, groups: GroupProvider, group: TeamGoogleGroup, person: Person
) -> bool:
    """Recheck the managed roster and current directory membership for a failed group."""
    if not person.active or group.team_id not in current_team_ids(storage, person.id):
        return False
    listed = groups.list_members(group.group_name).keys()
    owners = desired_members(storage, group.team_id)
    if listed - owners.keys() or owners.keys() - listed - set(group.rejected_additions):
        return False
    # Primary and verified-alternate addresses occupy separate unique namespaces.
    # A legacy collision can involve a former member or someone on a different team.
    for email in listed & person_emails(storage, person):
        if owners[email] != {person.primary_email}:
            continue
        primary = storage.get_person_by_email(email)
        alternate = storage.get_person_by_identifier("email", email)
        if {owner.id for owner in (primary, alternate) if owner} == {person.id}:
            # A sync may have invalidated the evidence while Google was responding.
            return storage.get_team_google_group(group.team_id) == group
    return False


def synced_groups_containing(
    storage: StorageAdapter,
    groups: GroupProvider,
    person: Person,
    *,
    include_individual: bool = False,
) -> list[TeamGoogleGroup]:
    """Google-listed memberships. Partial rosters require an explicit individual lookup."""
    emails = person_emails(storage, person)
    found = []
    for g in storage.list_team_google_groups():
        if (
            g.status == "synced"
            and g.group_name
            and emails & groups.list_members(g.group_name).keys()
        ):
            found.append(g)
        elif (
            include_individual
            and g.permits_individual_lookup
            and _confirmed_individual_member(storage, groups, g, person)
        ):
            # The people:read response does not expose another member's rejected address.
            found.append(
                g.model_copy(
                    update={"last_error": "Some Google membership additions were rejected."}
                )
            )
    return found


def sync_team(
    storage: StorageAdapter, groups: GroupProvider, team_id: UUID, *, actor: str
) -> TeamGoogleGroup | None:
    """Create the team's group if needed, then reconcile its members.

    Returns the recorded state, or None for an unknown team or a retired team
    that never had a group. Provider failures are recorded, never raised.

    The team lock is held from reading membership to recording the result, so
    a stale sync (e.g. the CLI cron) can't re-add someone a newer sync removed.
    """
    with _lock, storage.lock_team_google_group(team_id):
        team = storage.get_team(team_id)
        state = storage.get_team_google_group(team_id)
        if team is None or (not team.active and (state is None or state.group_name is None)):
            return state
        group_name = state.group_name if state else None
        # The address only becomes permanent once the group exists, so a failed
        # creation can be fixed by renaming the slug.
        email = state.group_email if group_name else f"{team.slug}@{groups.domain}"
        pending = [p.model_copy(deep=True) for p in state.pending_changes] if state else []

        def record(
            status: GoogleGroupStatus,
            error: str | None,
            *,
            rejected_additions: list[str] | None = None,
        ) -> TeamGoogleGroup:
            return storage.put_team_google_group(
                team_id,
                group_email=email,
                group_name=group_name,
                status=status,
                last_error=error,
                last_synced_at=(
                    datetime.now(timezone.utc)
                    if status == "synced"
                    else (state.last_synced_at if state else None)
                ),
                actor=actor,
                pending_changes=pending,
                rejected_additions=rejected_additions,
            )

        verified_omissions = []
        try:
            if group_name is None:
                group_name = groups.create_group(
                    email, team.label, f"Managed by Misty team-tracking (team {team.id})"
                )
            current = groups.list_members(group_name)
            # Cloud Identity has no operations.get method. A missing addition
            # or still-present removal is unresolved, even if today's desired
            # membership has reversed. An addition cannot be replayed safely:
            # the original could outlive a successful retry and later revocation.
            # Retry a removal against the same resource if still unwanted.
            # The exact resource matters if a different membership for the
            # same address has since been created.
            pending = [
                p
                for p in pending
                if (p.action == "add" and p.email not in current)
                or (p.action == "remove" and current.get(p.email) == p.membership_name)
            ]
            owners = desired_members(storage, team_id)
            want = owners.keys()
            rejected_adds: dict[str, str] = {}
            blocked = {p.email for p in pending if p.action == "add" or p.email in want}
            # Each change is independent: one bad address must never block
            # revoking someone else, so removals go first and errors accumulate.
            errors: list[str] = []
            external_blocked = False
            settings_ready = True
            if team.active:
                # Persist the new group's identity and invalidate a previous
                # synced result before a settings call can fail or be interrupted.
                record("failed", "Google group settings awaiting verification")
                try:
                    groups.ensure_external_members(email)
                except GroupProviderError as e:
                    settings_ready = False
                    errors.append(str(e))
            changes = [
                PendingGroupChange(action="remove", email=addr, membership_name=current[addr])
                for addr in sorted(current.keys() - want - blocked)
            ] + [
                PendingGroupChange(action="add", email=addr)
                for addr in sorted(want - current.keys() - blocked)
            ]
            for change in changes:
                if change.action == "add" and not settings_ready:
                    continue
                existing = next((p for p in pending if p.email == change.email), None)
                if existing is not None:
                    change = existing
                else:
                    pending.append(change)
                # Save before calling Google: a crash or a lost response must
                # leave enough state for another process to continue safely.
                record("failed", "Google membership change awaiting confirmation")
                try:
                    if change.action == "remove":
                        groups.remove_member(change.membership_name)
                    else:
                        groups.add_member(group_name, change.email)
                except GroupOperationPending as e:
                    change.operation_name = e.operation_name or change.operation_name
                    errors.append(str(e))
                except ExternalMembersDisabled:
                    if existing is None:
                        pending.remove(change)
                    external_blocked = True
                except MembershipIdentityRejected as e:
                    if existing is None:
                        pending.remove(change)
                    addr = change.email
                    if change.action == "add":
                        rejected_adds[addr] = str(e)
                    else:
                        errors.append(str(e))
                except GroupProviderError as e:
                    if existing is None:
                        pending.remove(change)
                    errors.append(str(e))
                else:
                    pending.remove(change)
            errors.extend(f"awaiting Google {p.action} completion for {p.email}" for p in pending)
            note = None
            if rejected_adds:
                if errors or external_blocked:
                    errors.extend(rejected_adds.values())
                else:
                    confirmed = groups.list_members(group_name).keys()
                    missing = want - confirmed
                    if confirmed - want or missing - rejected_adds.keys():
                        errors.extend(rejected_adds.values())
                        errors.append("expected Google membership not confirmed on readback")
                    elif missing:
                        covered = all(
                            addr.endswith("@mail.utoronto.ca")
                            and addr not in owners[addr]
                            and owners[addr] <= confirmed
                            for addr in missing
                        )
                        if covered:
                            note = (
                                f"Google declined {len(missing)} secondary university address(es). "
                                "Use the member's confirmed primary account for Drive access."
                            )
                        else:
                            verified_omissions = sorted(missing)
                            errors.extend(rejected_adds[addr] for addr in verified_omissions)
            if errors:
                hint = [EXTERNAL_MEMBERS_HINT] if external_blocked else []
                status, error = "failed", "; ".join(errors + hint)
            elif external_blocked:
                status, error = "needs_external_members", EXTERNAL_MEMBERS_HINT
            else:
                status, error = "synced", note
        except GroupProviderError as e:
            status, error = "failed", str(e)
        return record(status, error, rejected_additions=verified_omissions)


def current_team_ids(storage: StorageAdapter, person_id: UUID) -> set[UUID]:
    return {m.team_id for m in storage.list_memberships(person_id=person_id, as_of=date.today())}


def schedule_sync(
    background: BackgroundTasks,
    storage: StorageAdapter,
    groups: GroupProvider | None,
    team_ids: set[UUID],
    *,
    actor: str,
) -> None:
    """Sync after the response is sent. No-op when Google Groups is unconfigured."""
    if groups is None:
        return
    for team_id in team_ids:
        background.add_task(sync_team, storage, groups, team_id, actor=actor)
