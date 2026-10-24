from uuid import UUID
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, TypeAdapter
from typing import Literal

from contracts.directory import DirectoryUnavailable

_TIMEOUT = httpx.Timeout(5.0)


class _ChannelTeams(BaseModel):
    guild_id: str
    channel_id: str
    team_ids: list[UUID]


class _GroupState(BaseModel):
    team_id: UUID
    group_email: str = Field(min_length=1)
    group_name: str | None = None
    status: Literal["synced", "needs_external_members", "failed"]


class HttpDirectoryClient:
    """Validates directory ids and fetches display labels over team-tracking's
    HTTP API. A 404 means 'no such record' (returns None); connection failure
    or 5xx means 'directory unavailable' (raises DirectoryUnavailable)."""

    def __init__(self, base_url: str, api_key: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # A single reused httpx.Client keeps connections alive/pooled across
        # calls. Tests inject their own (MockTransport) client; production
        # callers get one shared instance (see src.api.deps.get_directory,
        # which caches the adapter so this client is built once).
        self._client = client or httpx.Client(timeout=_TIMEOUT)

    def _get_label(self, path: str, label_field: str) -> str | None:
        try:
            resp = self._client.get(f"{self._base_url}{path}", headers={"X-API-Key": self._api_key})
        except httpx.HTTPError as e:
            raise DirectoryUnavailable(f"directory unreachable: {e}") from e
        if resp.status_code == 404:
            return None
        if not (200 <= resp.status_code < 300):
            raise DirectoryUnavailable(f"directory returned {resp.status_code}")
        return resp.json().get(label_field)

    def get_team_label(self, team_id: UUID) -> str | None:
        return self._get_label(f"/teams/{team_id}", "label")

    def get_person_id_by_discord(self, discord_id: str) -> UUID | None:
        return self._get_id(f"/people/by-identifier/discord/{quote(discord_id, safe='')}")

    def get_meeting_audience(self, guild_id: str, channel_id: str) -> dict[UUID, str]:
        try:
            response = self._client.get(
                f"{self._base_url}/channels/{guild_id}/{channel_id}/teams",
                headers={"X-API-Key": self._api_key},
            )
            if response.status_code != 200:
                raise ValueError("channel unavailable")
            channel = _ChannelTeams.model_validate(response.json())
            if channel.guild_id != guild_id or channel.channel_id != channel_id:
                raise ValueError("wrong channel")
            if not channel.team_ids:
                return {}
            response = self._client.get(
                f"{self._base_url}/teams/google-groups", headers={"X-API-Key": self._api_key}
            )
            if response.status_code != 200:
                raise ValueError("groups unavailable")
            groups = TypeAdapter(list[_GroupState]).validate_python(response.json())
            if len({g.team_id for g in groups}) != len(groups):
                raise ValueError("duplicate group")
            audience = {
                g.team_id: g.group_email.lower()
                for g in groups
                if g.team_id in channel.team_ids and g.status == "synced" and g.group_name
            }
            return audience if set(audience) == set(channel.team_ids) else {}
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise DirectoryUnavailable("meeting audience unavailable") from None

    def get_person_label(self, person_id: UUID) -> str | None:
        return self._get_label(f"/people/{person_id}", "display_name")

    def get_active_team_ids(self, person_id: UUID) -> frozenset[UUID]:
        try:
            resp = self._client.get(
                f"{self._base_url}/memberships",
                params={"person_id": str(person_id), "active_only": "true"},
                headers={"X-API-Key": self._api_key},
            )
        except httpx.HTTPError as e:
            raise DirectoryUnavailable(f"directory unreachable: {e}") from e
        if not (200 <= resp.status_code < 300):
            raise DirectoryUnavailable(f"directory returned {resp.status_code}")
        try:
            return frozenset(UUID(m["team_id"]) for m in resp.json())
        except (KeyError, ValueError, TypeError) as e:
            raise DirectoryUnavailable(f"malformed memberships response: {e}") from e

    def _get_id(self, path: str) -> UUID | None:
        try:
            resp = self._client.get(f"{self._base_url}{path}", headers={"X-API-Key": self._api_key})
        except httpx.HTTPError as e:
            raise DirectoryUnavailable(f"directory unreachable: {e}") from e
        if resp.status_code == 404:
            return None
        if not (200 <= resp.status_code < 300):
            raise DirectoryUnavailable(f"directory returned {resp.status_code}")
        try:
            return UUID(resp.json()["id"])
        except (KeyError, ValueError, TypeError) as e:
            raise DirectoryUnavailable(f"malformed directory response: {e}") from e

    def get_person_id_by_verified_email(self, email: str) -> UUID | None:
        encoded = quote(email.strip().lower(), safe="")
        # primary_email is verified during registration; alternate `email`
        # identifiers can only be created by the verification-backed flow.
        person_id = self._get_id(f"/people/by-email/{encoded}")
        if person_id is not None:
            return person_id
        return self._get_id(f"/people/by-identifier/email/{encoded}")

    def get_team_id_by_synced_google_group(self, email: str) -> UUID | None:
        encoded = quote(email.strip().lower(), safe="")
        # Supplied by team-tracking issue #237. That endpoint is deliberately
        # defined to 404 unless the mapping is active AND membership sync is
        # healthy; documentation-system must not infer group membership from
        # an ordinary Team Tracking membership.
        return self._get_id(f"/teams/by-synced-google-group/{encoded}")

    def get_source_access_team_ids(self, person_id: UUID) -> frozenset[UUID]:
        try:
            resp = self._client.get(
                f"{self._base_url}/people/{person_id}/synced-google-teams",
                headers={"X-API-Key": self._api_key},
            )
        except httpx.HTTPError as e:
            raise DirectoryUnavailable(f"directory unreachable: {e}") from e
        if resp.status_code == 404:
            return frozenset()
        if not (200 <= resp.status_code < 300):
            raise DirectoryUnavailable(f"directory returned {resp.status_code}")
        try:
            return frozenset(UUID(item["team_id"]) for item in resp.json())
        except (KeyError, ValueError, TypeError) as e:
            raise DirectoryUnavailable(f"malformed synced Google teams response: {e}") from e

    def get_channel_source_team_ids(self, guild_id: str, channel_id: str) -> frozenset[UUID]:
        try:
            response = self._client.get(
                f"{self._base_url}/channels/{guild_id}/{channel_id}/teams",
                headers={"X-API-Key": self._api_key},
            )
            # 404 is not the unconfigured contract (#238 returns 200 + []).
            if response.status_code != 200:
                raise DirectoryUnavailable("channel scope unavailable")
            channel = _ChannelTeams.model_validate(response.json())
            if channel.guild_id != guild_id or channel.channel_id != channel_id:
                raise ValueError("wrong channel scope")
            if not channel.team_ids:
                return frozenset()
            response = self._client.get(
                f"{self._base_url}/teams/google-groups",
                headers={"X-API-Key": self._api_key},
            )
            if response.status_code != 200:
                raise DirectoryUnavailable("channel groups unavailable")
            groups = TypeAdapter(list[_GroupState]).validate_python(response.json())
            if len({group.team_id for group in groups}) != len(groups):
                raise ValueError("duplicate group state")
            return frozenset(
                group.team_id
                for group in groups
                if group.team_id in channel.team_ids
                and group.status == "synced"
                and group.group_name
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise DirectoryUnavailable("channel scope unavailable") from None
