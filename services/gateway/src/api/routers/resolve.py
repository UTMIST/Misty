from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from contracts.directory import DirectoryClient, DirectoryUnavailable
from src.api.auth import AuthedKey, require_scope
from src.api.deps import get_directory
from src.api.ratelimit import enforce_key_rate_limit

# One body for both misses. Splitting them — "github login not found" versus
# "no discord identifier for that github login" — told an external caller
# whether a given GitHub user is in the directory at all. That is a membership
# oracle on a public endpoint, and enumerating it is cheap: exactly the kind of
# leak this service exists to prevent. The two cases stay distinguishable in the
# audit log, where only we can read them.
_NOT_FOUND = "no discord id for that github login"


class DiscordId(BaseModel):
    """The entire public response. Nothing else about the person leaves here."""

    model_config = ConfigDict(extra="forbid")
    discord_id: str


router = APIRouter(
    prefix="/v1",
    tags=["resolve"],
    # The per-consumer quota, applied to every /v1 route. It runs after
    # require_api_key resolves the caller, so it meters an issued key rather
    # than an attacker-supplied header; see src/api/ratelimit.py. Wrong-scope
    # requests are metered too — they are still requests we had to authenticate.
    dependencies=[Depends(enforce_key_rate_limit)],
)


@router.get("/resolve/discord/{github_login}", response_model=DiscordId)
def resolve_discord(
    github_login: str,
    directory: DirectoryClient = Depends(get_directory),
    _: AuthedKey = Depends(require_scope("resolve:discord")),
) -> DiscordId:
    # `github_login` reaches the audit log, because it is a path segment and
    # AuditLogMiddleware records request.url.path. That is deliberate: it is
    # what makes abuse of the public door investigable, the value is public and
    # pseudonymous, and the caller supplied it in the first place. What never
    # joins it there is anything the directory told us back.
    person = directory.get_person_by_github(github_login)
    if person is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    person_id = person.get("id")
    if person_id is None:
        # The directory answered, but not in a shape we understand. That is an
        # upstream fault rather than a missing record, so fail closed to 503
        # instead of raising KeyError into a 500.
        raise DirectoryUnavailable("person record has no id")
    identifiers = directory.list_identifiers(person_id)
    discord_id = next(
        (i.get("external_id") for i in identifiers if i.get("provider") == "discord"), None
    )
    if discord_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return DiscordId(discord_id=discord_id)
