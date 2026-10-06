"""Cloud Identity Groups API implementation of contracts.groups.GroupProvider.

Authenticates as the group-owning user (an OAuth refresh token), because
that is the setup verified in issue #236. Google libraries are imported
lazily so the dependency never loads when Google Groups is unconfigured.
"""

from contracts.groups import ExternalMembersDisabled, GroupProviderError

API = "https://cloudidentity.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups"
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleGroupsProvider:
    def __init__(
        self,
        *,
        customer_id: str,
        domain: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        session=None,
        timeout_s: float = 30.0,
    ) -> None:
        self.domain = domain
        self._parent = f"customers/{customer_id}"
        self._timeout_s = timeout_s
        if session is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.credentials import Credentials

            session = AuthorizedSession(
                Credentials(
                    token=None,
                    refresh_token=refresh_token,
                    client_id=client_id,
                    client_secret=client_secret,
                    token_uri=TOKEN_URI,
                    scopes=[SCOPE],
                )
            )
        self._session = session

    def _call(self, method: str, path: str, **kwargs):
        """Returns (status, json body). Transport and token-refresh failures
        become GroupProviderError; HTTP error statuses are left to the caller."""
        try:
            resp = self._session.request(method, f"{API}/{path}", timeout=self._timeout_s, **kwargs)
        except Exception as e:  # requests.RequestException, google.auth RefreshError
            raise GroupProviderError(f"google request failed: {type(e).__name__}") from e
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body

    @staticmethod
    def _error(status: int, body: dict, what: str) -> GroupProviderError:
        message = (body.get("error") or {}).get("message", "")
        return GroupProviderError(f"{what}: HTTP {status} {message}".strip())

    def create_group(self, email: str, display_name: str, description: str) -> str:
        status, body = self._call(
            "POST",
            "groups",
            params={"initialGroupConfig": "WITH_INITIAL_OWNER"},
            json={
                "parent": self._parent,
                "groupKey": {"id": email},
                "displayName": display_name,
                "description": description,
                "labels": {"cloudidentity.googleapis.com/groups.discussion_forum": ""},
            },
        )
        if status == 409:
            return self._adopt(email, description)
        if status >= 400:
            raise self._error(status, body, "create group")
        if body.get("error"):
            raise GroupProviderError(f"create group: {body['error'].get('message', '')}")
        name = (body.get("response") or {}).get("name")
        if not body.get("done") or not name:
            # The next sync's create gets a 409 and adopts the finished group.
            raise GroupProviderError("create group: operation not finished; retry")
        return name

    def _adopt(self, email: str, description: str) -> str:
        status, body = self._call("GET", "groups:lookup", params={"groupKey.id": email})
        name = body.get("name") if status == 200 else None
        if name:
            status, group = self._call("GET", name)
            if status == 200 and group.get("description") == description:
                return name
        raise GroupProviderError(f"create group: {email} is already in use by another group")

    def list_members(self, group_name: str) -> dict[str, str]:
        members: dict[str, str] = {}
        params = {"view": "BASIC", "pageSize": 1000}
        while True:
            status, body = self._call("GET", f"{group_name}/memberships", params=params)
            if status >= 400:
                raise self._error(status, body, "list members")
            for m in body.get("memberships", []):
                roles = {r.get("name") for r in m.get("roles", [])}
                if roles == {"MEMBER"}:
                    members[m["preferredMemberKey"]["id"].lower()] = m["name"]
            token = body.get("nextPageToken")
            if not token:
                return members
            params = {**params, "pageToken": token}

    def add_member(self, group_name: str, email: str) -> None:
        status, body = self._call(
            "POST",
            f"{group_name}/memberships",
            json={"preferredMemberKey": {"id": email}, "roles": [{"name": "MEMBER"}]},
        )
        if status < 400 or status == 409:
            return
        message = (body.get("error") or {}).get("message", "").lower()
        # ponytail: matched on message text; #236 only recorded that the error
        # says the group disallows members "outside" the organization. Update
        # this if Google's wording changes.
        if "outside" in message or "external" in message:
            raise ExternalMembersDisabled(message)
        raise self._error(status, body, f"add member {email}")

    def remove_member(self, membership_name: str) -> None:
        status, body = self._call("DELETE", membership_name)
        if status < 400 or status == 404:
            return
        raise self._error(status, body, "remove member")
