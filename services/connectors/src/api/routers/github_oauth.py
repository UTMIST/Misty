import hashlib
import hmac
import json

from starlette.concurrency import run_in_threadpool

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from contracts.github_oauth import GitHubLinkConfirm, GitHubLinkSubject, GitHubOAuthError
from src.api.auth import require_scope
from src.api.deps import get_github_linking
from src.config import get_settings

router = APIRouter(prefix="/github/oauth", tags=["github-linking"])


def _call(fn):
    try:
        return fn()
    except GitHubOAuthError as e:
        code = (
            503
            if str(e) in ("not_configured", "vault_unavailable", "directory_unavailable")
            else 409
        )
        raise HTTPException(code, str(e), headers={"Cache-Control": "no-store"}) from None


@router.post("/start")
def start(
    body: GitHubLinkSubject,
    response: Response,
    _key=Depends(require_scope("github-links:write")),
    service=Depends(get_github_linking),
):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: service.start(str(body.person_id), body.discord_id))


@router.post("/confirm")
def confirm(
    body: GitHubLinkConfirm,
    response: Response,
    _key=Depends(require_scope("github-links:write")),
    service=Depends(get_github_linking),
):
    response.headers["Cache-Control"] = "no-store"
    return _call(
        lambda: service.confirm(
            str(body.person_id), body.discord_id, body.confirmation_code.get_secret_value()
        )
    )


@router.post("/status")
def status(
    body: GitHubLinkSubject,
    response: Response,
    _key=Depends(require_scope("github-links:read")),
    service=Depends(get_github_linking),
):
    response.headers["Cache-Control"] = "no-store"
    return _call(lambda: service.status(str(body.person_id), body.discord_id))


@router.post("/unlink")
def unlink(
    body: GitHubLinkSubject,
    _key=Depends(require_scope("github-links:write")),
    service=Depends(get_github_linking),
):
    return _call(lambda: service.unlink(str(body.person_id), body.discord_id))


@router.get("/callback")
def callback(
    state: str = Query(min_length=1, max_length=200),
    code: str = Query(default="", max_length=500),
    service=Depends(get_github_linking),
):
    # Public endpoint: single-use state binds to the initiated person/Discord account.
    result = _call(lambda: service.callback(state, code))
    return Response(
        "Return to the same Discord account and run /github action:confirm code:"
        + result["confirmation_code"]
        + "\nThis private completion code expires in five minutes. Do not share it.",
        media_type="text/plain",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def verified_revocation(request: Request):
    secret = get_settings().github_oauth_webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(503, "not_configured")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, "payload too large")
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(body)
        if (
            request.headers.get("X-GitHub-Event") == "github_app_authorization"
            and payload.get("action") == "revoked"
        ):
            github_id = payload["sender"]["id"]
            if not isinstance(github_id, int) or github_id <= 0:
                raise ValueError()
            return github_id
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "invalid event") from None
    return None


@router.post("/revoked")
async def revoked(
    github_id: int | None = Depends(verified_revocation), service=Depends(get_github_linking)
):
    if github_id is not None:
        await run_in_threadpool(_call, lambda: service.revoked(github_id))
    return Response(status_code=204)
