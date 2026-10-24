"""Cloud Identity Groups API implementation of contracts.groups.GroupProvider.

Authenticates as the group-owning user (an OAuth refresh token), because
that is the setup verified in issue #236. Google libraries are imported
lazily so the dependency never loads when Google Groups is unconfigured.
"""

from urllib.parse import quote

from contracts.groups import ExternalMembersDisabled, GroupOperationPending, GroupProviderError

API = "https://cloudidentity.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/cloud-identity.groups"
SETTINGS_API = "https://www.googleapis.com/groups/v1/groups"
SETTINGS_SCOPE = "https://www.googleapis.com/auth/apps.groups.settings"
EXTERNAL_SETTINGS = {
    "allowExternalMembers": "true",
    "whoCanAddExternalMembers": "END_USERS_CAN_ADD_EXTERNAL_MEMBERS",
}
TOKEN_URI = "https://oauth2.googleapis.com/token"
# google.rpc.Code values carried in an Operation's `error.code`
NOT_FOUND = 5
ALREADY_EXISTS = 6


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
        settings_session=None,
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
            # Keep membership authorization independent: a missing Settings
            # grant must not prevent revocations with the existing Groups grant.
            if settings_session is None:
                settings_session = AuthorizedSession(
                    Credentials(
                        token=None,
                        refresh_token=refresh_token,
                        client_id=client_id,
                        client_secret=client_secret,
                        token_uri=TOKEN_URI,
                        scopes=[SETTINGS_SCOPE],
                    )
                )
        self._session = session
        self._settings_session = settings_session if settings_session is not None else session

    def _settings_call(self, method: str, email: str, **kwargs) -> dict:
        try:
            response = self._settings_session.request(
                method,
                f"{SETTINGS_API}/{quote(email, safe='')}",
                params={"alt": "json"},
                timeout=self._timeout_s,
                allow_redirects=False,
                **kwargs,
            )
        except Exception as e:
            # Settings PATCH is idempotent, unlike membership writes. A normal
            # retry reads its effect first; it needs no pending membership intent.
            raise GroupProviderError(
                f"group settings: {type(e).__name__}; verify Settings API authorization and retry sync"
            ) from e
        if response.status_code != 200:
            raise GroupProviderError(
                f"group settings: HTTP {response.status_code}; "
                "check Settings API enablement, apps.groups.settings scope and Google privileges; "
                "retry sync"
            )
        try:
            body = response.json()
        except ValueError:
            raise GroupProviderError("group settings: invalid JSON; retry sync") from None
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("email"), str)
            or body["email"].lower() != email.lower()
        ):
            raise GroupProviderError("group settings: unexpected group response; retry sync")
        allowed = body.get("allowExternalMembers")
        who = body.get("whoCanAddExternalMembers")
        # Google omits this dependent setting on fresh groups while external
        # membership is disabled. Configure both, then require both on read-back.
        omitted_while_disabled = allowed == "false" and "whoCanAddExternalMembers" not in body
        if allowed not in ("true", "false") or (
            who
            not in ("END_USERS_CAN_ADD_EXTERNAL_MEMBERS", "ONLY_ADMINS_CAN_ADD_EXTERNAL_MEMBERS")
            and not omitted_while_disabled
        ):
            raise GroupProviderError("group settings: incomplete settings response; retry sync")
        return body

    def ensure_external_members(self, group_email: str) -> None:
        current = self._settings_call("GET", group_email)
        changes = {k: v for k, v in EXTERNAL_SETTINGS.items() if current.get(k) != v}
        if changes:
            self._settings_call("PATCH", group_email, json=changes)
            current = self._settings_call("GET", group_email)
        if any(current[k] != v for k, v in EXTERNAL_SETTINGS.items()):
            raise GroupProviderError(
                "group settings: external membership settings not confirmed; "
                "ask UTMIST's Google administrator to check policy, then retry sync"
            )

    def _call(self, method: str, path: str, **kwargs):
        """Returns (status, json body). Transport and token-refresh failures
        become domain errors. Uncertain writes stay pending; other HTTP error
        statuses are left to the caller."""
        try:
            resp = self._session.request(method, f"{API}/{path}", timeout=self._timeout_s, **kwargs)
        except Exception as e:  # requests and google.auth transport/refresh errors
            from google.auth.exceptions import RefreshError, TransportError
            from requests.exceptions import ConnectTimeout

            # Auth refresh fails before sending the write; ConnectTimeout
            # guarantees no request was sent. Other transport errors may follow
            # an accepted write, so keep its intent until the effect is known.
            error = (
                GroupProviderError
                if method == "GET" or isinstance(e, (RefreshError, TransportError, ConnectTimeout))
                else GroupOperationPending
            )
            raise error(f"google request failed: {type(e).__name__}") from e
        if method != "GET" and resp.status_code >= 500:
            raise GroupOperationPending(f"google write outcome unknown: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            if method == "GET" and resp.status_code < 400:
                raise GroupProviderError("google read returned invalid JSON") from None
            body = {}
        if not isinstance(body, dict):
            error = (
                GroupOperationPending
                if method != "GET" and resp.status_code < 400
                else GroupProviderError
            )
            raise error("google response was not a JSON object")
        return resp.status_code, body

    @staticmethod
    def _error(status: int, body: dict, what: str) -> GroupProviderError:
        err = body.get("error") or {}
        where = f"HTTP {status}" if status >= 400 else f"operation error {err.get('code', '')}"
        return GroupProviderError(f"{what}: {where} {err.get('message', '')}".strip())

    def _finished(self, status: int, body: dict, what: str) -> dict:
        """Return a long-running Operation's `response`. Raises unless the call
        succeeded and the Operation is `done` without an `error`. An unfinished
        write must remain recorded until a later sync observes its effect."""
        if status >= 400 or body.get("error"):
            raise self._error(status, body, what)
        if not body.get("done"):
            raise GroupOperationPending(
                f"{what}: operation not finished; retry", operation_name=body.get("name")
            )
        return body.get("response") or {}

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
        # An unfinished create is retried: the next sync gets a 409 and adopts it.
        name = self._finished(status, body, "create group").get("name")
        if not name:
            raise GroupProviderError("create group: operation returned no group; retry")
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
        err = body.get("error") or {}
        if status == 409 or err.get("code") == ALREADY_EXISTS:
            return
        message = err.get("message", "").lower()
        # ponytail: matched on message text; #236 only recorded that the error
        # says the group disallows members "outside" the organization. Update
        # this if Google's wording changes.
        if "outside" in message or "external" in message:
            raise ExternalMembersDisabled(message)
        self._finished(status, body, f"add member {email}")

    def remove_member(self, membership_name: str) -> None:
        status, body = self._call("DELETE", membership_name)
        if status == 404 or (body.get("error") or {}).get("code") == NOT_FOUND:
            return
        self._finished(status, body, "remove member")
