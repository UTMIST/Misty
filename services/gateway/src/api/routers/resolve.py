from fastapi import APIRouter, Depends, HTTPException, status

from contracts.directory import DirectoryClient
from src.api.auth import AuthedKey, require_scope
from src.api.deps import get_directory

router = APIRouter(prefix="/v1", tags=["resolve"])


@router.get("/resolve/discord/{github_login}")
def resolve_discord(
    github_login: str,
    directory: DirectoryClient = Depends(get_directory),
    _: AuthedKey = Depends(require_scope("resolve:discord")),
) -> dict[str, str]:
    person = directory.get_person_by_github(github_login)
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="github login not found")
    ids = directory.list_identifiers(person["id"])
    discord_id = next((i["external_id"] for i in ids if i.get("provider") == "discord"), None)
    if discord_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no discord identifier for that github login",
        )
    return {"discord_id": discord_id}
