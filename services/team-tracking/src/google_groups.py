"""Reconcile each team's managed Google Group with Team Tracking membership.

Team Tracking is the source of truth. A sync computes the emails that should
be in the group, reads who actually is, and adds/removes the difference, so
every run is idempotent and retrying is just running it again.
"""

import threading
from datetime import date, datetime, timezone
from uuid import UUID

from fastapi import BackgroundTasks

from contracts.groups import ExternalMembersDisabled, GroupProvider, GroupProviderError
from contracts.storage import StorageAdapter
from contracts.types import TeamGoogleGroup

# ponytail: one process-wide lock, so two syncs of a team can't interleave and
# re-add someone a newer sync removed. Holds only because production runs a
# single uvicorn process; with more workers, switch to pg_advisory_lock per team.
_lock = threading.Lock()

EXTERNAL_MEMBERS_HINT = (
    "group rejects external members: a group owner must enable "
    "'Allow external members' in Google Groups, then re-sync"
)


def desired_emails(storage: StorageAdapter, team_id: UUID) -> set[str]:
    """Every email of every active person with a membership current today."""
    team = storage.get_team(team_id)
    if team is None or not team.active:
        return set()
    emails: set[str] = set()
    for m in storage.list_memberships(team_id=team_id, as_of=date.today()):
        person = storage.get_person(m.person_id)
        if person is None or not person.active:
            continue
        emails.add(person.primary_email)
        emails.update(
            i.external_id
            for i in storage.list_person_identifiers(person.id)
            if i.provider == "email"
        )
    return emails


def sync_team(
    storage: StorageAdapter, groups: GroupProvider, team_id: UUID, *, actor: str
) -> TeamGoogleGroup | None:
    """Create the team's group if needed, then reconcile its members.

    Returns the recorded state, or None for an unknown team or a retired team
    that never had a group. Provider failures are recorded, never raised.
    """
    with _lock:
        team = storage.get_team(team_id)
        state = storage.get_team_google_group(team_id)
        if team is None or (not team.active and (state is None or state.group_name is None)):
            return state
        group_name = state.group_name if state else None
        # The address only becomes permanent once the group exists, so a failed
        # creation can be fixed by renaming the slug.
        email = state.group_email if group_name else f"{team.slug}@{groups.domain}"
        try:
            if group_name is None:
                group_name = groups.create_group(
                    email, team.label, f"Managed by Misty team-tracking (team {team.id})"
                )
            current = groups.list_members(group_name)
            want = desired_emails(storage, team_id)
            external_blocked = False
            for addr in sorted(want - current.keys()):
                try:
                    groups.add_member(group_name, addr)
                except ExternalMembersDisabled:
                    external_blocked = True
            for addr in sorted(current.keys() - want):
                groups.remove_member(current[addr])
            if external_blocked:
                status, error = "needs_external_members", EXTERNAL_MEMBERS_HINT
            else:
                status, error = "synced", None
        except GroupProviderError as e:
            status, error = "failed", str(e)
        synced_at = datetime.now(timezone.utc) if status == "synced" else None
        return storage.put_team_google_group(
            team_id,
            group_email=email,
            group_name=group_name,
            status=status,
            last_error=error,
            last_synced_at=synced_at or (state.last_synced_at if state else None),
            actor=actor,
        )


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
