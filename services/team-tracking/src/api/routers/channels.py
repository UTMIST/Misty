from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from contracts.storage import StorageAdapter
from contracts.types import ChannelTeams, ChannelTeamsReplace
from src.api.auth import AuthedKey, get_actor, require_scope
from src.api.deps import get_storage

router = APIRouter(prefix="/channels", tags=["channels"])

# Discord snowflakes.
GuildId = Path(pattern=r"^\d+$")
ChannelId = Path(pattern=r"^\d+$")


@router.get("/{guild_id}/{channel_id}/teams", response_model=ChannelTeams)
def get_channel_teams(
    guild_id: str = GuildId,
    channel_id: str = ChannelId,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("channels:read")),
) -> ChannelTeams:
    return storage.get_channel_teams(guild_id, channel_id)


@router.put("/{guild_id}/{channel_id}/teams", response_model=ChannelTeams)
def replace_channel_teams(
    payload: ChannelTeamsReplace,
    guild_id: str = GuildId,
    channel_id: str = ChannelId,
    storage: StorageAdapter = Depends(get_storage),
    actor: str = Depends(get_actor),
    _: AuthedKey = Depends(require_scope("channels:write")),
) -> ChannelTeams:
    try:
        return storage.replace_channel_teams(guild_id, channel_id, payload.team_ids, actor=actor)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/{guild_id}/{channel_id}/teams", status_code=status.HTTP_204_NO_CONTENT)
def clear_channel_teams(
    guild_id: str = GuildId,
    channel_id: str = ChannelId,
    storage: StorageAdapter = Depends(get_storage),
    _: AuthedKey = Depends(require_scope("channels:write")),
) -> Response:
    storage.clear_channel_teams(guild_id, channel_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
