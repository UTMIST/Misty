import time
from urllib.parse import urlencode

import httpx

from contracts.github_oauth import GitHubOAuthError


class GitHubOAuth:
    def __init__(self, *, client_id, client_secret, callback_url, client=None, clock=time.time):
        self.client_id, self.client_secret, self.callback_url = (
            client_id,
            client_secret,
            callback_url,
        )
        self.client = client or httpx.Client(timeout=15, follow_redirects=False)
        self.clock = clock

    def authorize_url(self, state, challenge):
        return "https://github.com/login/oauth/authorize?" + urlencode(
            dict(
                client_id=self.client_id,
                redirect_uri=self.callback_url,
                state=state,
                code_challenge=challenge,
                code_challenge_method="S256",
                scope="offline_access",
            )
        )

    def _json(self, method, url, **kwargs):
        try:
            with self.client.stream(method, url, **kwargs) as r:
                if r.status_code not in (200, 201):
                    raise GitHubOAuthError("github_authorization_failed")
                chunks = bytearray()
                for chunk in r.iter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 65536:
                        raise GitHubOAuthError("github_response_invalid")
                import json

                data = json.loads(chunks)
                if not isinstance(data, dict):
                    raise GitHubOAuthError("github_response_invalid")
                return data
        except (httpx.HTTPError, ValueError):
            raise GitHubOAuthError("github_authorization_failed") from None

    def _tokens(self, fields):
        data = self._json(
            "POST",
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data=dict(client_id=self.client_id, client_secret=self.client_secret, **fields),
        )
        try:
            if data.get("error") or not data["access_token"] or not data["refresh_token"]:
                raise ValueError()
            if not all(
                isinstance(data[k], str) and len(data[k]) <= 8192
                for k in ("access_token", "refresh_token")
            ):
                raise ValueError()
            expires = int(data["expires_in"])
            refresh_expires = int(data["refresh_token_expires_in"])
            if expires <= 0 or refresh_expires <= 0:
                raise ValueError()
            return dict(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=self.clock() + expires,
                refresh_expires_at=self.clock() + refresh_expires,
            )
        except (KeyError, TypeError, ValueError):
            raise GitHubOAuthError("expiring_tokens_required") from None

    def exchange(self, code, verifier):
        return self._tokens(dict(code=code, code_verifier=verifier, redirect_uri=self.callback_url))

    def refresh(self, refresh_token):
        return self._tokens(dict(grant_type="refresh_token", refresh_token=refresh_token))

    def profile(self, token):
        data = self._json(
            "GET",
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        try:
            user_id, login = data["id"], data["login"]
            if (
                not isinstance(user_id, int)
                or user_id <= 0
                or not isinstance(login, str)
                or not login
            ):
                raise ValueError()
            return dict(github_id=user_id, login=login)
        except (KeyError, TypeError, ValueError):
            raise GitHubOAuthError("github_profile_invalid") from None

    def revoke(self, token):
        # DELETE /applications/{client_id}/token revokes only this token.
        try:
            r = self.client.request(
                "DELETE",
                f"https://api.github.com/applications/{self.client_id}/token",
                auth=(self.client_id, self.client_secret),
                json={"access_token": token},
            )
            if r.status_code not in (204, 404, 422):
                raise GitHubOAuthError("github_revocation_pending")
        except httpx.HTTPError:
            raise GitHubOAuthError("github_revocation_pending") from None
