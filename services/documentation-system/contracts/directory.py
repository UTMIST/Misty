from typing import Protocol
from uuid import UUID


class DirectoryUnavailable(Exception):
    """Raised when team-tracking cannot be reached (connection/timeout/5xx).
    The ingest layer degrades: stores the id with a null label + a warning."""


class DirectoryClient(Protocol):
    def get_meeting_audience(self, guild_id: str, channel_id: str) -> dict[UUID, str]:
        """All channel teams mapped to healthy Google Groups, or {} if any
        mapping is missing/unsynced. Outage raises DirectoryUnavailable.
        """
        ...

    def get_person_id_by_discord(self, discord_id: str) -> UUID | None: ...

    def get_channel_source_team_ids(self, guild_id: str, channel_id: str) -> frozenset[UUID]:
        """Resolve active configured teams with healthy managed Google Groups.

        Empty for an unconfigured channel. Raises DirectoryUnavailable for any
        unverified/malformed scope, never falls back to all teams.
        """
        ...

    def get_team_label(self, team_id: UUID) -> str | None:
        """Return the team's display label, or None if no such team.
        Raises DirectoryUnavailable if the directory cannot be reached."""
        ...

    def get_person_label(self, person_id: UUID) -> str | None:
        """Return the person's display name, or None if no such person.
        Raises DirectoryUnavailable if the directory cannot be reached."""
        ...

    def get_active_team_ids(self, person_id: UUID) -> frozenset[UUID]:
        """Return the set of team_ids the person is an active member of.
        Raises DirectoryUnavailable if the directory cannot be reached."""
        ...

    def get_person_id_by_verified_email(self, email: str) -> UUID | None:
        """Resolve a primary or verified additional email to a person.
        Returns None for an unknown email; raises DirectoryUnavailable on failure."""
        ...

    def get_team_id_by_synced_google_group(self, email: str) -> UUID | None:
        """Resolve a managed source grant usable by at least the individual lookup.

        A known rejected addition may preserve the grant for confirmed people.
        Other failed and legacy mappings return None. Raises DirectoryUnavailable when sync state cannot be verified.
        """
        ...

    def get_source_access_team_ids(self, person_id: UUID) -> frozenset[UUID]:
        """Teams whose managed Google Group currently contains this person
        and whose sync supports individual access. Never infer from directory-only
        membership. Raises DirectoryUnavailable if state cannot be verified."""
        ...
